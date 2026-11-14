from __future__ import annotations

import heapq
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from time import perf_counter

from app.domain.models import RouteQuery, SeatPrice, TrainSegment, TransferPlan
from app.services.route_search_engine import StreamSnapshot
from app.services.route_search_plans import plan_within_duration_limit
from app.services.static_price_repository import SQLiteStaticPriceRepository, TrainOdFareEdge, TrainOdPriceSnapshot


MAX_RETURNED_PLANS = 10


@dataclass(frozen=True)
class TimeExpandedSearchState:
    station: str
    arrive_at: datetime | None
    total_price: Decimal
    segments: tuple[TrainSegment, ...]

    @property
    def transfer_count(self) -> int:
        return max(0, len(self.segments) - 1)


class TimeExpandedRouteSearchEngine:
    def __init__(self, repository: SQLiteStaticPriceRepository, provider: str = "12306-public-price") -> None:
        self.repository = repository
        self.provider = provider

    @property
    def source(self) -> str:
        return "time-expanded"

    @property
    def status(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "search_engine": "time-expanded",
            "status": "ok",
            "online_remote_io": False,
        }

    async def stream_snapshots(self, query: RouteQuery) -> AsyncIterator[StreamSnapshot]:
        started_at = perf_counter()
        plans = await self._search_plans(query)
        yield {
            "type": "snapshot",
            "query_id": f"{query.date.isoformat()}:{query.from_station}:{query.to_station}",
            "source": self.source,
            "updated_at": datetime.now(timezone.utc),
            "elapsed_ms": max(0, round((perf_counter() - started_at) * 1000)),
            "plans": plans,
            "searched_count": 0,
            "pending_count": 0,
            "remote_query_count": 0,
            "final": True,
        }

    async def _search_plans(self, query: RouteQuery) -> list[TransferPlan]:
        rows = self._load_edges(query)
        edges_by_station: dict[str, list[TrainOdPriceSnapshot]] = defaultdict(list)
        for row in rows:
            edges_by_station[row.from_station].append(row)

        departures_by_station: dict[str, list[datetime]] = {}
        for station, edges in edges_by_station.items():
            edges.sort(key=lambda edge: (edge.depart_at, edge.arrive_at, edge.train_code, edge.seat_type))
            # 与 edges 下标一一对应的出发时刻表，供二分定位"能赶上的第一班"。
            departures_by_station[station] = [edge.depart_at for edge in edges]

        plans: list[TransferPlan] = []
        queue: list[tuple[Decimal, int, TimeExpandedSearchState]] = []
        counter = 0
        initial_state = TimeExpandedSearchState(
            station=query.from_station,
            arrive_at=None,
            total_price=Decimal("0"),
            segments=(),
        )
        heapq.heappush(queue, (Decimal("0"), counter, initial_state))

        # (车站, 到达时刻) -> 已经花掉的最低价。同一站、同一到达时刻，价格更高的
        # 那条不可能拼出更便宜的后半程，直接剪掉。
        # 旧实现把"完整路径"放进状态键，等于每换一种车次组合就算一个新状态，
        # 真实数据下会组合爆炸（北京出站 4744 条边 × 3 天 ≈ 1.4 万个起点）。
        best_price_per_arrival: dict[tuple[str, datetime], Decimal] = {}

        # 已经收集到的第 10 便宜方案的价格（用负值堆，堆顶就是"最贵的那条"）。
        # 堆里的价格是随扩展单调不减的：后生成的方案只会更贵，所以一旦队首价格
        # 已经超过第 10 名，剩下的方案再便宜也挤不进前 10，可以直接收工。
        # 比较用严格大于，价格相同的方案仍然会被展开，避免改动并列时的排序结果。
        plan_price_heap: list[Decimal] = []

        while queue:
            price, _, state = heapq.heappop(queue)

            if len(plan_price_heap) >= MAX_RETURNED_PLANS and price > -plan_price_heap[0]:
                break

            if state.arrive_at is not None:
                slot = (state.station, state.arrive_at)
                known_price = best_price_per_arrival.get(slot)
                if known_price is not None and price >= known_price:
                    continue
                best_price_per_arrival[slot] = price

            start, end = self._candidate_range(state, query, departures_by_station)
            for index in range(start, end):
                edge = edges_by_station[state.station][index]
                if not self._can_take(edge, state, query):
                    continue

                next_segment = self._build_segment(edge)
                next_segments = (*state.segments, next_segment)
                next_price = price + edge.price

                if len(next_segments) - 1 > query.max_transfers:
                    continue

                if edge.to_station == query.to_station:
                    plan = self._build_plan(list(next_segments))
                    if plan_within_duration_limit(plan, query):
                        plans.append(plan)
                        if len(plan_price_heap) < MAX_RETURNED_PLANS:
                            heapq.heappush(plan_price_heap, -plan.total_price)
                        elif plan.total_price < -plan_price_heap[0]:
                            heapq.heapreplace(plan_price_heap, -plan.total_price)
                    continue

                next_state = TimeExpandedSearchState(
                    station=edge.to_station,
                    arrive_at=edge.arrive_at,
                    total_price=next_price,
                    segments=next_segments,
                )
                counter += 1
                heapq.heappush(queue, (next_price, counter, next_state))

        plans = self._keep_best_plan_per_train_sequence(plans)
        plans.sort(key=self._plan_sort_key)
        return plans[:MAX_RETURNED_PLANS]

    def _keep_best_plan_per_train_sequence(self, plans: list[TransferPlan]) -> list[TransferPlan]:
        """同一组车次只保留最优的一条。

        因为基础边会按 0/1/2 天偏移展开，同一趟车可能派生出多个变体：
        当天 9 小时直达的，和"在长沙住一晚再走"的 33 小时方案。它们车次相同、
        价格相同，却各占一个返回名额，会把直达和更多换乘形态挤出前 10。
        """
        best_plans: dict[tuple[str, ...], TransferPlan] = {}
        for plan in plans:
            signature = tuple(segment.train_no for segment in plan.segments)
            current = best_plans.get(signature)
            if current is None or self._plan_sort_key(plan) < self._plan_sort_key(current):
                best_plans[signature] = plan
        return list(best_plans.values())

    def _load_edges(self, query: RouteQuery) -> list[TrainOdPriceSnapshot]:
        fare_edges = self.repository.query_train_od_fare_edges(self.provider)
        if fare_edges:
            return [
                materialized
                for edge in fare_edges
                for materialized in self._materialize_fare_edge(edge, query)
            ]

        service_dates = [query.date + timedelta(days=offset) for offset in range(3)]
        return self.repository.query_train_od_prices(self.provider, service_dates)

    def _materialize_fare_edge(self, edge: TrainOdFareEdge, query: RouteQuery) -> list[TrainOdPriceSnapshot]:
        materialized: list[TrainOdPriceSnapshot] = []
        for base_day_offset in range(3):
            base_date = query.date + timedelta(days=base_day_offset)
            depart_at = datetime.combine(
                base_date + timedelta(days=edge.depart_day_offset),
                edge.depart_time,
                tzinfo=timezone.utc,
            )
            arrive_at = datetime.combine(
                base_date + timedelta(days=edge.arrive_day_offset),
                edge.arrive_time,
                tzinfo=timezone.utc,
            )
            if arrive_at <= depart_at:
                arrive_at += timedelta(days=1)
            materialized.append(
                TrainOdPriceSnapshot(
                    provider=edge.provider,
                    service_date=base_date,
                    train_no=edge.train_no,
                    train_code=edge.train_code,
                    from_station=edge.from_station,
                    to_station=edge.to_station,
                    from_station_no=edge.from_station_no,
                    to_station_no=edge.to_station_no,
                    depart_at=depart_at,
                    arrive_at=arrive_at,
                    duration_minutes=edge.duration_minutes,
                    seat_type=edge.seat_type,
                    price=edge.price,
                    source=edge.source,
                    fetched_at=edge.fetched_at,
                )
            )
        return materialized

    def _candidate_range(
        self,
        state: TimeExpandedSearchState,
        query: RouteQuery,
        departures_by_station: dict[str, list[datetime]],
    ) -> tuple[int, int]:
        """用二分圈出这一站"能赶上的车"的下标范围，避免每次扫全部出站边。

        出站边已按 depart_at 排序，所以只要把"最早可乘"和"最晚可乘"两个
        时刻放进 bisect，就能直接拿到切片。`_can_take` 仍会再校验一次，
        二分只是把候选量从"这一站所有车"降到"时间窗内的车"。
        """
        departures = departures_by_station.get(state.station)
        if not departures:
            return 0, 0

        if state.arrive_at is None:
            # 首段只能是查询当天：时间窗就是那一天。
            earliest = datetime.combine(query.date, time.min, tzinfo=timezone.utc)
            latest = earliest + timedelta(days=1)
        else:
            earliest = state.arrive_at + timedelta(minutes=query.min_transfer_minutes)
            latest = state.arrive_at + timedelta(minutes=query.max_transfer_minutes)

        return bisect_left(departures, earliest), bisect_right(departures, latest)

    def _can_take(self, edge: TrainOdPriceSnapshot, state: TimeExpandedSearchState, query: RouteQuery) -> bool:
        if state.arrive_at is None:
            # 首段必须在查询当天出发。基础边会按 0/1/2 天偏移展开成多份，
            # 但"用户查哪天"就是"哪天走"，把次日的车次也算进来会让同一趟车
            # 变出多天方案，占满返回名额。
            return edge.depart_at.date() == query.date
        earliest_departure = state.arrive_at + timedelta(minutes=query.min_transfer_minutes)
        # 换乘等待也有上限：等到次日下午甚至第三天的"方案"虽然便宜，
        # 实际没人会选，还会把当天可行的方案挤出返回名额。
        latest_departure = state.arrive_at + timedelta(minutes=query.max_transfer_minutes)
        return earliest_departure <= edge.depart_at <= latest_departure

    def _build_segment(self, edge: TrainOdPriceSnapshot) -> TrainSegment:
        return TrainSegment(
            train_no=edge.train_no,
            from_station=edge.from_station,
            to_station=edge.to_station,
            depart_at=edge.depart_at,
            arrive_at=edge.arrive_at,
            duration_minutes=edge.duration_minutes,
            prices=[SeatPrice(seat_type=edge.seat_type, price=edge.price, remaining="unknown")],
            source=edge.source,
            updated_at=edge.fetched_at,
        )

    def _build_plan(self, segments: list[TrainSegment]) -> TransferPlan:
        total_price = sum((segment.lowest_price or Decimal("0") for segment in segments), Decimal("0"))
        transfer_stations = [segment.to_station for segment in segments[:-1]]
        transfer_minutes = 0
        for previous, current in zip(segments, segments[1:]):
            transfer_minutes += max(0, int((current.depart_at - previous.arrive_at).total_seconds() // 60))
        total_duration_minutes = int((segments[-1].arrive_at - segments[0].depart_at).total_seconds() // 60)
        return TransferPlan(
            total_price=total_price,
            total_duration_minutes=total_duration_minutes,
            transfer_minutes=transfer_minutes,
            transfer_stations=transfer_stations,
            segments=segments,
        )

    def _plan_sort_key(self, plan: TransferPlan) -> tuple[Decimal, int, int, int]:
        return (plan.total_price, len(plan.transfer_stations), plan.transfer_minutes, plan.total_duration_minutes)