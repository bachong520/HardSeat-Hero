import asyncio
import heapq
import os
from bisect import bisect_left, bisect_right
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.adapters.base import TrainDataProvider, TrainDataProviderError
from app.domain.models import RouteQuery, TrainSegment, TransferPlan
from app.services.cache import SqliteOdCache, TtlCache
from app.services.route_search_engine import StreamSnapshot
from app.services.route_search_models import (
    OnlineSearchContext,
    OnlineSearchParetoFrontier,
    OnlineSearchState,
    SearchDiagnostics,
)
from app.services.route_search_plans import (
    build_plan,
    deduplicate_plans,
    plan_identity_key,
    plan_sort_key,
    plan_within_duration_limit,
    transfer_wait_comfort_penalty,
)
from app.services.route_search_serializers import deserialize_train_segments, serialize_train_segments
from app.services.search_telemetry import SqliteSearchTelemetryRecorder


MAX_RETURNED_PLANS = 10
class OnlineRouteSearchEngine:
    def __init__(
        self,
        provider: TrainDataProvider,
        cache_ttl_seconds: int = 600,
        max_remote_queries: int = 120,
        max_concurrent_remote_queries: int = 5,
        remote_query_interval_seconds: float | None = None,
        persistent_cache: SqliteOdCache[list[TrainSegment]] | None = None,
        telemetry_recorder: SqliteSearchTelemetryRecorder | None = None,
    ) -> None:
        self.provider = provider
        self.segment_cache: TtlCache[list[TrainSegment]] = TtlCache(cache_ttl_seconds)
        self.persistent_segment_cache = persistent_cache or self._default_persistent_cache(cache_ttl_seconds)
        self.max_remote_queries = max_remote_queries
        self.max_concurrent_remote_queries = max_concurrent_remote_queries
        self._remote_query_semaphore = asyncio.Semaphore(max_concurrent_remote_queries)
        self._remote_query_lock = asyncio.Lock()
        self.remote_query_interval_seconds = (
            remote_query_interval_seconds if remote_query_interval_seconds is not None else self._default_remote_query_interval()
        )
        self.telemetry_recorder = telemetry_recorder or self._default_telemetry_recorder()
        self.remote_query_count = 0
        self.last_diagnostics = SearchDiagnostics()
        self._last_diagnostics_lock = asyncio.Lock()

    @property
    def source(self) -> str:
        return self.provider.name

    @property
    def status(self) -> dict[str, object]:
        return {
            "provider": self.provider.name,
            "search_engine": "online",
            "expansion_strategy": "candidate-station-od",
            "online_remote_io": self.provider.name != "mock",
            "global_optimum_guaranteed": False,
            "status": "ok",
            "transfer_candidate_enabled": self.provider.name != "mock",
            "max_remote_queries": self.max_remote_queries,
            "max_concurrent_remote_queries": self.max_concurrent_remote_queries,
            "last_remote_query_count": self.remote_query_count,
            "last_diagnostics": self.last_diagnostics.as_dict(),
        }

    async def stream_snapshots(self, query: RouteQuery) -> AsyncIterator[StreamSnapshot]:
        context = OnlineSearchContext()
        candidate_count = len(self.provider.candidate_transfer_stations(query)) if query.max_transfers >= 1 else 0

        plans = await self._search_plans(query, context)
        final_plans = self._deduplicate_plans(plans)
        final_plans.sort(key=self._plan_sort_key)
        final_plans = final_plans[:MAX_RETURNED_PLANS]
        await self._finish_search(query, final_plans, context)
        final_snapshot = self._build_stream_snapshot(query, final_plans, context, candidate_count)
        final_snapshot["final"] = True
        yield final_snapshot

    async def _search_plans(self, query: RouteQuery, context: OnlineSearchContext) -> list[TransferPlan]:
        candidates = self._ranked_expansion_stations(query)
        best_price: Decimal | None = None
        plans: list[TransferPlan] = []
        queue: list[tuple[Decimal, int, OnlineSearchState]] = []
        counter = 0
        initial_state = OnlineSearchState(
            current_station=query.from_station,
            segments=(),
            accumulated_price=Decimal("0"),
            arrive_at=None,
            visited_stations=frozenset({query.from_station}),
        )
        heapq.heappush(queue, (Decimal("0"), counter, initial_state))
        frontier = OnlineSearchParetoFrontier()

        while queue:
            priority, _, state = heapq.heappop(queue)
            if best_price is not None and priority > best_price:
                break

            for next_station in self._next_expansion_stations(state, query, candidates):
                self._record_expanded_candidate(context, next_station)
                try:
                    segments = await self._candidate_segments(state, next_station, query, context)
                except TrainDataProviderError:
                    self._record_failed_candidate(context, next_station)
                    continue

                for segment in segments:
                    next_price = state.accumulated_price + (segment.lowest_price or Decimal("0"))
                    next_segments = (*state.segments, segment)
                    next_transfer_count = max(0, len(next_segments) - 1)
                    if next_transfer_count > query.max_transfers:
                        continue
                    if next_station == query.to_station:
                        plan = self._build_plan(list(next_segments))
                        if self._plan_within_duration_limit(plan, query):
                            best_price = plan.total_price if best_price is None else min(best_price, plan.total_price)
                            plans.append(plan)
                        continue

                    if best_price is not None and next_price > best_price:
                        context.diagnostics.pruned_by_complete_price_bound_count += 1
                        continue

                    next_state = OnlineSearchState(
                        current_station=next_station,
                        segments=next_segments,
                        accumulated_price=next_price,
                        arrive_at=segment.arrive_at,
                        visited_stations=state.visited_stations | {next_station},
                    )
                    if not frontier.add_if_not_dominated(next_state):
                        context.diagnostics.pruned_by_pareto_count += 1
                        continue
                    counter += 1
                    heapq.heappush(queue, (next_price, counter, next_state))

        plans.sort(key=self._plan_sort_key)
        return plans[:MAX_RETURNED_PLANS]

    def _next_expansion_stations(
        self,
        state: OnlineSearchState,
        query: RouteQuery,
        candidates: list[str],
    ) -> list[str]:
        stations = [query.to_station]
        if len(state.segments) < query.max_transfers:
            stations.extend(station for station in candidates if station not in state.visited_stations)
        return stations

    async def _candidate_segments(
        self,
        state: OnlineSearchState,
        next_station: str,
        query: RouteQuery,
        context: OnlineSearchContext,
    ) -> list[TrainSegment]:
        day_offsets = (0,) if not state.segments else (0, 1)
        segments = await self._search_transfer_window_segments(
            state.current_station,
            next_station,
            query,
            day_offsets=day_offsets,
            context=context,
        )
        segments = self._priced_segments_sorted_by_departure(segments)
        if state.arrive_at is None:
            return segments
        earliest_departure = state.arrive_at + timedelta(minutes=query.min_transfer_minutes)
        latest_departure = state.arrive_at + timedelta(minutes=query.max_transfer_minutes)
        depart_at_list = [segment.depart_at for segment in segments]
        start_index = bisect_left(depart_at_list, earliest_departure)
        end_index = bisect_right(depart_at_list, latest_departure)
        return segments[start_index:end_index]

    async def _search_segments(
        self,
        from_station: str,
        to_station: str,
        query: RouteQuery,
        context: OnlineSearchContext,
    ) -> list[TrainSegment]:
        cache_key = self._segment_cache_key(from_station, to_station, query)
        cached_segments = self.segment_cache.get(cache_key)
        if cached_segments is not None:
            context.diagnostics.memory_cache_hit_count += 1
            return cached_segments

        if self.persistent_segment_cache is not None:
            persisted_segments = self.persistent_segment_cache.get(cache_key)
            if persisted_segments is not None:
                context.diagnostics.persistent_cache_hit_count += 1
                self.segment_cache.set(cache_key, persisted_segments)
                return persisted_segments

        async with context.remote_query_lock:
            if context.remote_query_count >= self.max_remote_queries:
                return []

            should_wait = self.remote_query_interval_seconds > 0 and context.remote_query_count > 0
            context.remote_query_count += 1
            context.diagnostics.remote_query_count = context.remote_query_count

        if should_wait:
            await asyncio.sleep(self.remote_query_interval_seconds)

        async with self._remote_query_semaphore:
            segments = await self.provider.search_segments(from_station, to_station, query)
        self.segment_cache.set(cache_key, segments)
        if self.persistent_segment_cache is not None:
            self.persistent_segment_cache.set(cache_key, segments)
        return segments

    def _ranked_expansion_stations(self, query: RouteQuery) -> list[str]:
        blocked_stations = {query.from_station, query.to_station}
        provider_candidates = [
            station
            for station in self.provider.candidate_transfer_stations(query)
            if station not in blocked_stations
        ]
        provider_order = {station: index for index, station in enumerate(provider_candidates)}
        telemetry_hits = []
        if self.telemetry_recorder is not None:
            telemetry_hits = [
                hit
                for hit in self.telemetry_recorder.transfer_station_hits(
                    self.provider.name,
                    query.from_station,
                    query.to_station,
                )
                if hit.station not in blocked_stations
            ]
        hit_by_station = {hit.station: hit for hit in telemetry_hits}
        candidates = list(dict.fromkeys([*(hit.station for hit in telemetry_hits), *provider_candidates]))

        def sort_key(station: str) -> tuple[int, int, Decimal, int, str]:
            hit = hit_by_station.get(station)
            if hit is None:
                return (1, 0, Decimal("Infinity"), provider_order.get(station, len(provider_order)), station)
            return (
                0,
                -hit.hit_count,
                hit.best_price if hit.best_price is not None else Decimal("Infinity"),
                provider_order.get(station, len(provider_order)),
                station,
            )

        candidates.sort(key=sort_key)
        return candidates

    async def _search_transfer_window_segments(
        self,
        from_station: str,
        to_station: str,
        query: RouteQuery,
        day_offsets: tuple[int, ...],
        context: OnlineSearchContext,
    ) -> list[TrainSegment]:
        segments: list[TrainSegment] = []
        for offset in day_offsets:
            window_query = query.model_copy(update={"date": query.date + timedelta(days=offset)})
            segments.extend(await self._search_segments(from_station, to_station, window_query, context))
        return segments

    def _priced_segments_sorted_by_departure(self, segments: list[TrainSegment]) -> list[TrainSegment]:
        return sorted(
            [segment for segment in segments if segment.lowest_price is not None],
            key=lambda segment: segment.depart_at,
        )

    def _plan_sort_key(self, plan: TransferPlan) -> tuple[Decimal, int, int, int]:
        return plan_sort_key(plan)

    def _plan_within_duration_limit(self, plan: TransferPlan, query: RouteQuery) -> bool:
        return plan_within_duration_limit(plan, query)

    def _deduplicate_plans(self, plans: list[TransferPlan]) -> list[TransferPlan]:
        return deduplicate_plans(plans)

    def _plan_identity_key(self, plan: TransferPlan) -> tuple[tuple[str, str, str, datetime, datetime], ...]:
        return plan_identity_key(plan)

    def _build_stream_snapshot(
        self,
        query: RouteQuery,
        plans: list[TransferPlan],
        context: OnlineSearchContext,
        candidate_count: int,
    ) -> StreamSnapshot:
        searched_count = len(context.diagnostics.expanded_od_targets)
        failed_count = len(context.diagnostics.failed_od_targets)
        return {
            "type": "snapshot",
            "query_id": f"{query.date.isoformat()}:{query.from_station}:{query.to_station}",
            "source": self.provider.name,
            "updated_at": datetime.now(timezone.utc),
            "plans": plans,
            "searched_count": searched_count,
            "pending_count": max(0, candidate_count - searched_count - failed_count),
            "remote_query_count": context.remote_query_count,
        }

    def _transfer_wait_comfort_penalty(self, plan: TransferPlan) -> int:
        return transfer_wait_comfort_penalty(plan)

    def _build_plan(self, segments: list[TrainSegment]) -> TransferPlan:
        return build_plan(segments)

    def _segment_cache_key(self, from_station: str, to_station: str, query: RouteQuery) -> str:
        return ":".join(
            [
                self.provider.name,
                query.date.isoformat(),
                from_station,
                to_station,
            ]
        )

    def _default_remote_query_interval(self) -> float:
        if self.provider.name == "mock":
            return 0
        return 0.05

    def _default_persistent_cache(self, ttl_seconds: int) -> SqliteOdCache[list[TrainSegment]] | None:
        cache_path = os.getenv("ROUTE_SEGMENT_CACHE_DB", "").strip()
        if not cache_path:
            return None
        cache_ttl_seconds = int(os.getenv("ROUTE_SEGMENT_CACHE_TTL_SECONDS", str(ttl_seconds)))
        return SqliteOdCache[list[TrainSegment]](
            Path(cache_path),
            ttl_seconds=cache_ttl_seconds,
            serializer=serialize_train_segments,
            deserializer=deserialize_train_segments,
        )

    def _default_telemetry_recorder(self) -> SqliteSearchTelemetryRecorder | None:
        telemetry_path = os.getenv("SEARCH_TELEMETRY_DB", "").strip()
        if not telemetry_path:
            return None
        return SqliteSearchTelemetryRecorder(Path(telemetry_path))

    async def _finish_search(self, query: RouteQuery, plans: list[TransferPlan], context: OnlineSearchContext) -> None:
        async with self._last_diagnostics_lock:
            self.remote_query_count = context.remote_query_count
            self.last_diagnostics = context.diagnostics
        self._record_search(query, plans, context)

    def _record_search(self, query: RouteQuery, plans: list[TransferPlan], context: OnlineSearchContext) -> None:
        if self.telemetry_recorder is None:
            return
        self.telemetry_recorder.record_search(
            query=query,
            provider=self.provider.name,
            plans=plans,
            remote_query_count=context.remote_query_count,
        )

    def _record_expanded_candidate(self, context: OnlineSearchContext, station: str) -> None:
        if station not in context.diagnostics.expanded_od_targets:
            context.diagnostics.expanded_od_targets.append(station)

    def _record_failed_candidate(self, context: OnlineSearchContext, station: str) -> None:
        if station not in context.diagnostics.failed_od_targets:
            context.diagnostics.failed_od_targets.append(station)
