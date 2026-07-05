import asyncio
import heapq
import json
import os
from bisect import bisect_left
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import NotRequired, TypedDict

from app.adapters.base import TrainDataProvider, TrainDataProviderError
from app.domain.models import RouteQuery, RouteSearchResponse, TrainSegment, TransferPlan
from app.services.cache import SqliteOdCache, TtlCache
from app.services.search_telemetry import SqliteSearchTelemetryRecorder


MAX_RETURNED_PLANS = 10
COMFORTABLE_MIN_TRANSFER_MINUTES = 30
COMFORTABLE_MAX_TRANSFER_MINUTES = 120


class StreamSnapshot(TypedDict):
    type: str
    query_id: str
    source: str
    updated_at: datetime
    plans: list[TransferPlan]
    searched_count: int
    pending_count: int
    remote_query_count: int
    final: NotRequired[bool]


@dataclass
class SearchDiagnostics:
    remote_query_count: int = 0
    memory_cache_hit_count: int = 0
    persistent_cache_hit_count: int = 0
    expanded_candidates: list[str] = field(default_factory=list)
    failed_candidates: list[str] = field(default_factory=list)
    pruned_by_best_price_count: int = 0
    pruned_by_pareto_count: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "remote_query_count": self.remote_query_count,
            "memory_cache_hit_count": self.memory_cache_hit_count,
            "persistent_cache_hit_count": self.persistent_cache_hit_count,
            "expanded_candidates": self.expanded_candidates,
            "failed_candidates": self.failed_candidates,
            "pruned_by_best_price_count": self.pruned_by_best_price_count,
            "pruned_by_pareto_count": self.pruned_by_pareto_count,
        }


class SearchContext:
    def __init__(self) -> None:
        self.remote_query_count = 0
        self.diagnostics = SearchDiagnostics()
        self.remote_query_lock = asyncio.Lock()


class BestPriceTracker:
    def __init__(self) -> None:
        self._value: Decimal | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> Decimal | None:
        async with self._lock:
            return self._value

    async def update(self, price: Decimal) -> None:
        async with self._lock:
            if self._value is None or price < self._value:
                self._value = price


@dataclass(frozen=True)
class PathSearchState:
    current_station: str
    segments: tuple[TrainSegment, ...]
    accumulated_price: Decimal
    arrive_at: datetime | None
    visited_stations: frozenset[str]

    @property
    def transfer_count(self) -> int:
        return max(0, len(self.segments) - 1)


@dataclass(frozen=True)
class FrontierEntry:
    price: Decimal
    arrive_at: datetime


class ParetoFrontier:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, int], list[FrontierEntry]] = {}

    def add_if_not_dominated(self, state: PathSearchState) -> bool:
        if state.arrive_at is None:
            return True

        key = (state.current_station, state.transfer_count)
        entries = self._entries.setdefault(key, [])
        for entry in entries:
            if entry.price <= state.accumulated_price and entry.arrive_at <= state.arrive_at:
                return False

        entries[:] = [
            entry
            for entry in entries
            if not (state.accumulated_price <= entry.price and state.arrive_at <= entry.arrive_at)
        ]
        entries.append(FrontierEntry(price=state.accumulated_price, arrive_at=state.arrive_at))
        return True


class CheapestPathSearcher:
    def __init__(self, route_search: "ProviderBackedRouteSearchEngine") -> None:
        self.route_search = route_search

    async def search(self, query: RouteQuery, context: SearchContext) -> list[TransferPlan]:
        candidates = self._ranked_transfer_candidates(query)
        best_price: Decimal | None = None
        plans: list[TransferPlan] = []
        queue: list[tuple[Decimal, int, PathSearchState]] = []
        counter = 0
        initial_state = PathSearchState(
            current_station=query.from_station,
            segments=(),
            accumulated_price=Decimal("0"),
            arrive_at=None,
            visited_stations=frozenset({query.from_station}),
        )
        heapq.heappush(queue, (Decimal("0"), counter, initial_state))
        frontier = ParetoFrontier()
        while queue:
            priority, _, state = heapq.heappop(queue)
            if best_price is not None and priority > best_price:
                break

            for next_station in self._next_stations(state, query, candidates):
                self.route_search._record_expanded_candidate(context, next_station)
                try:
                    segments = await self._candidate_segments(state, next_station, query, context)
                except TrainDataProviderError:
                    self.route_search._record_failed_candidate(context, next_station)
                    continue

                for segment in segments:
                    next_price = state.accumulated_price + (segment.lowest_price or Decimal("0"))
                    if best_price is not None and next_price > best_price:
                        context.diagnostics.pruned_by_best_price_count += 1
                        continue
                    next_segments = (*state.segments, segment)
                    if next_station == query.to_station:
                        plan = self.route_search._build_plan(list(next_segments))
                        if self.route_search._plan_within_duration_limit(plan, query):
                            best_price = plan.total_price if best_price is None else min(best_price, plan.total_price)
                            plans.append(plan)
                        continue

                    next_state = PathSearchState(
                        current_station=next_station,
                        segments=next_segments,
                        accumulated_price=next_price,
                        arrive_at=segment.arrive_at,
                        visited_stations=state.visited_stations | {next_station},
                    )
                    if next_state.transfer_count > query.max_transfers:
                        continue
                    if not frontier.add_if_not_dominated(next_state):
                        context.diagnostics.pruned_by_pareto_count += 1
                        continue
                    counter += 1
                    heapq.heappush(queue, (next_price + self._heuristic_lower_bound(next_station, query.to_station), counter, next_state))

        plans.sort(key=self.route_search._plan_sort_key)
        return plans[:MAX_RETURNED_PLANS]

    def _next_stations(self, state: PathSearchState, query: RouteQuery, candidates: list[str]) -> list[str]:
        stations = [query.to_station]
        if state.transfer_count < query.max_transfers:
            stations.extend(station for station in candidates if station not in state.visited_stations)
        return stations

    def _ranked_transfer_candidates(self, query: RouteQuery) -> list[str]:
        return self.route_search._ranked_transfer_candidates(query)

    async def _candidate_segments(
        self,
        state: PathSearchState,
        next_station: str,
        query: RouteQuery,
        context: SearchContext,
    ) -> list[TrainSegment]:
        day_offsets = (0,) if not state.segments else (0, 1)
        segments = await self.route_search._search_transfer_window_segments(
            state.current_station,
            next_station,
            query,
            day_offsets=day_offsets,
            context=context,
        )
        segments = self.route_search._priced_segments_sorted_by_departure(segments)
        if state.arrive_at is None:
            return segments
        earliest_departure = state.arrive_at + timedelta(minutes=query.min_transfer_minutes)
        start_index = bisect_left([segment.depart_at for segment in segments], earliest_departure)
        return segments[start_index:]

    def _heuristic_lower_bound(self, from_station: str, to_station: str) -> Decimal:
        return Decimal("0")


class ProviderBackedRouteSearchEngine:
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
        self.cheapest_path_searcher = CheapestPathSearcher(self)
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
            "search_engine": "candidate",
            "status": "ok",
            "transfer_candidate_enabled": self.provider.name != "mock",
            "max_remote_queries": self.max_remote_queries,
            "max_concurrent_remote_queries": self.max_concurrent_remote_queries,
            "last_remote_query_count": self.remote_query_count,
            "last_diagnostics": self.last_diagnostics.as_dict(),
        }

    async def search(self, query: RouteQuery) -> RouteSearchResponse:
        started_at = perf_counter()
        context = SearchContext()
        plans = [plan async for plan in self._iter_plans(query, context)]
        plans = self._deduplicate_plans(plans)
        plans.sort(key=self._plan_sort_key)
        plans = plans[:MAX_RETURNED_PLANS]
        await self._finish_search(query, plans, context)

        return RouteSearchResponse(
            query_id=f"{query.date.isoformat()}:{query.from_station}:{query.to_station}",
            source=self.provider.name,
            updated_at=datetime.now(timezone.utc),
            elapsed_ms=max(0, round((perf_counter() - started_at) * 1000)),
            plans=plans,
        )

    async def stream(self, query: RouteQuery) -> AsyncIterator[TransferPlan]:
        context = SearchContext()
        emitted: list[TransferPlan] = []
        emitted_keys: set[tuple[tuple[str, str, str, datetime, datetime], ...]] = set()
        async for plan in self._iter_plans(query, context):
            plan_key = self._plan_identity_key(plan)
            if plan_key in emitted_keys:
                continue
            emitted_keys.add(plan_key)
            emitted.append(plan)
            emitted.sort(key=self._plan_sort_key)
            if plan in emitted[:MAX_RETURNED_PLANS]:
                yield plan
        await self._finish_search(query, emitted[:MAX_RETURNED_PLANS], context)

    async def stream_snapshots(self, query: RouteQuery) -> AsyncIterator[StreamSnapshot]:
        context = SearchContext()
        plans: list[TransferPlan] = []
        plan_keys: set[tuple[tuple[str, str, str, datetime, datetime], ...]] = set()
        last_top_keys: tuple[tuple[tuple[str, str, str, datetime, datetime], ...], ...] = ()
        candidate_count = len(self.provider.candidate_transfer_stations(query)) if query.max_transfers >= 1 else 0

        async for plan in self._iter_plans(query, context):
            plan_key = self._plan_identity_key(plan)
            if plan_key in plan_keys:
                continue
            plan_keys.add(plan_key)
            plans.append(plan)
            plans.sort(key=self._plan_sort_key)
            top_plans = plans[:MAX_RETURNED_PLANS]
            top_keys = tuple(self._plan_identity_key(top_plan) for top_plan in top_plans)
            if top_keys != last_top_keys:
                last_top_keys = top_keys
                yield self._build_stream_snapshot(query, top_plans, context, candidate_count)

        final_plans = plans[:MAX_RETURNED_PLANS]
        await self._finish_search(query, final_plans, context)
        final_snapshot = self._build_stream_snapshot(query, final_plans, context, candidate_count)
        final_snapshot["final"] = True
        yield final_snapshot

    async def _iter_plans(self, query: RouteQuery, context: SearchContext) -> AsyncIterator[TransferPlan]:
        if query.max_transfers >= 2:
            for plan in await self.cheapest_path_searcher.search(query, context):
                yield plan
            return

        best_price = BestPriceTracker()
        direct_segments = await self._search_segments(query.from_station, query.to_station, query, context)
        for segment in direct_segments:
            if segment.lowest_price is not None:
                plan = self._build_plan([segment])
                if self._plan_within_duration_limit(plan, query):
                    await best_price.update(plan.total_price)
                    yield plan

        if query.max_transfers >= 1:
            async for plan in self._iter_one_transfer(query, best_price, context):
                await best_price.update(plan.total_price)
                yield plan

    async def _search_segments(
        self,
        from_station: str,
        to_station: str,
        query: RouteQuery,
        context: SearchContext,
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

    async def _iter_one_transfer(
        self,
        query: RouteQuery,
        best_price: BestPriceTracker,
        context: SearchContext,
    ) -> AsyncIterator[TransferPlan]:
        tasks = [
            asyncio.create_task(self._one_transfer_plans_for_station(query, transfer_station, best_price, context))
            for transfer_station in self._ranked_transfer_candidates(query)
        ]
        for task in asyncio.as_completed(tasks):
            try:
                plans = await task
            except TrainDataProviderError:
                continue
            for plan in plans:
                yield plan

    def _ranked_transfer_candidates(self, query: RouteQuery) -> list[str]:
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

    async def _one_transfer_plans_for_station(
        self,
        query: RouteQuery,
        transfer_station: str,
        best_price: BestPriceTracker,
        context: SearchContext,
    ) -> list[TransferPlan]:
        self._record_expanded_candidate(context, transfer_station)
        first_legs = await self._search_transfer_window_segments(
            query.from_station,
            transfer_station,
            query,
            day_offsets=(-1, 0),
            context=context,
        )
        first_legs = self._priced_segments_sorted_by_arrival(first_legs)
        if not first_legs:
            return []

        current_best_price = await best_price.get()
        first_legs = self._segments_that_can_improve_best_price(first_legs, current_best_price)
        if not first_legs:
            if current_best_price is not None:
                context.diagnostics.pruned_by_best_price_count += 1
            return []

        second_legs = await self._search_transfer_window_segments(
            transfer_station,
            query.to_station,
            query,
            day_offsets=(0, 1),
            context=context,
        )
        second_legs = self._priced_segments_sorted_by_departure(second_legs)
        if not second_legs:
            return []

        plans: list[TransferPlan] = []
        second_departures = [segment.depart_at for segment in second_legs]
        for first in first_legs:
            current_best_price = await best_price.get()
            if first.lowest_price is not None and current_best_price is not None and first.lowest_price > current_best_price:
                context.diagnostics.pruned_by_best_price_count += 1
                continue
            earliest_departure = first.arrive_at + timedelta(minutes=query.min_transfer_minutes)
            start_index = bisect_left(second_departures, earliest_departure)
            for second in second_legs[start_index:]:
                plan = self._build_plan([first, second])
                if self._plan_within_duration_limit(plan, query):
                    current_best_price = await best_price.get()
                    if current_best_price is not None and plan.total_price > current_best_price:
                        context.diagnostics.pruned_by_best_price_count += 1
                        continue
                    await best_price.update(plan.total_price)
                    plans.append(plan)
        return plans

    async def _search_transfer_window_segments(
        self,
        from_station: str,
        to_station: str,
        query: RouteQuery,
        day_offsets: tuple[int, ...],
        context: SearchContext,
    ) -> list[TrainSegment]:
        segments: list[TrainSegment] = []
        for offset in day_offsets:
            window_query = query.model_copy(update={"date": query.date + timedelta(days=offset)})
            segments.extend(await self._search_segments(from_station, to_station, window_query, context))
        return segments

    def _priced_segments_sorted_by_arrival(self, segments: list[TrainSegment]) -> list[TrainSegment]:
        return sorted(
            [segment for segment in segments if segment.lowest_price is not None],
            key=lambda segment: segment.arrive_at,
        )

    def _priced_segments_sorted_by_departure(self, segments: list[TrainSegment]) -> list[TrainSegment]:
        return sorted(
            [segment for segment in segments if segment.lowest_price is not None],
            key=lambda segment: segment.depart_at,
        )

    def _segments_that_can_improve_best_price(
        self,
        segments: list[TrainSegment],
        best_price: Decimal | None,
    ) -> list[TrainSegment]:
        if best_price is None:
            return segments
        return [segment for segment in segments if segment.lowest_price is not None and segment.lowest_price <= best_price]

    def _plan_sort_key(self, plan: TransferPlan) -> tuple[Decimal, int, int, int]:
        return (
            plan.total_price,
            self._transfer_wait_comfort_penalty(plan),
            len(plan.transfer_stations),
            plan.total_duration_minutes,
        )

    def _plan_within_duration_limit(self, plan: TransferPlan, query: RouteQuery) -> bool:
        if query.max_total_duration_minutes is None:
            return True
        return plan.total_duration_minutes <= query.max_total_duration_minutes

    def _deduplicate_plans(self, plans: list[TransferPlan]) -> list[TransferPlan]:
        unique_plans: list[TransferPlan] = []
        seen: set[tuple[tuple[str, str, str, datetime, datetime], ...]] = set()
        for plan in plans:
            plan_key = self._plan_identity_key(plan)
            if plan_key in seen:
                continue
            seen.add(plan_key)
            unique_plans.append(plan)
        return unique_plans

    def _plan_identity_key(self, plan: TransferPlan) -> tuple[tuple[str, str, str, datetime, datetime], ...]:
        return tuple(
            (
                segment.train_no,
                segment.from_station,
                segment.to_station,
                segment.depart_at,
                segment.arrive_at,
            )
            for segment in plan.segments
        )

    def _build_stream_snapshot(
        self,
        query: RouteQuery,
        plans: list[TransferPlan],
        context: SearchContext,
        candidate_count: int,
    ) -> StreamSnapshot:
        searched_count = len(context.diagnostics.expanded_candidates)
        failed_count = len(context.diagnostics.failed_candidates)
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
        penalty = 0
        for previous, current in zip(plan.segments, plan.segments[1:]):
            wait_minutes = max(0, int((current.depart_at - previous.arrive_at).total_seconds() // 60))
            if wait_minutes < COMFORTABLE_MIN_TRANSFER_MINUTES:
                penalty += (COMFORTABLE_MIN_TRANSFER_MINUTES - wait_minutes) * 10
            elif wait_minutes > COMFORTABLE_MAX_TRANSFER_MINUTES:
                penalty += wait_minutes - COMFORTABLE_MAX_TRANSFER_MINUTES
        return penalty

    def _build_plan(self, segments: list[TrainSegment]) -> TransferPlan:
        total_price = sum([segment.lowest_price or Decimal("0") for segment in segments], Decimal("0"))
        total_duration = max(0, int((segments[-1].arrive_at - segments[0].depart_at).total_seconds() // 60))
        ride_minutes = sum(segment.duration_minutes for segment in segments)
        transfer_stations = [segment.to_station for segment in segments[:-1]]
        return TransferPlan(
            total_price=total_price,
            total_duration_minutes=total_duration,
            transfer_minutes=max(0, total_duration - ride_minutes),
            transfer_stations=transfer_stations,
            segments=segments,
        )

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

    async def _finish_search(self, query: RouteQuery, plans: list[TransferPlan], context: SearchContext) -> None:
        async with self._last_diagnostics_lock:
            self.remote_query_count = context.remote_query_count
            self.last_diagnostics = context.diagnostics
        self._record_search(query, plans, context)

    def _record_search(self, query: RouteQuery, plans: list[TransferPlan], context: SearchContext) -> None:
        if self.telemetry_recorder is None:
            return
        self.telemetry_recorder.record_search(
            query=query,
            provider=self.provider.name,
            plans=plans,
            remote_query_count=context.remote_query_count,
        )

    def _record_expanded_candidate(self, context: SearchContext, station: str) -> None:
        if station not in context.diagnostics.expanded_candidates:
            context.diagnostics.expanded_candidates.append(station)

    def _record_failed_candidate(self, context: SearchContext, station: str) -> None:
        if station not in context.diagnostics.failed_candidates:
            context.diagnostics.failed_candidates.append(station)


def serialize_train_segments(segments: list[TrainSegment]) -> str:
    return json.dumps([segment.model_dump(mode="json") for segment in segments], ensure_ascii=False)


def deserialize_train_segments(value: str) -> list[TrainSegment]:
    payload = json.loads(value)
    if not isinstance(payload, list):
        raise ValueError("cached train segments must be a list")
    return [TrainSegment.model_validate(item) for item in payload]
