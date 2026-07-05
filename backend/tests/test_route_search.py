import asyncio
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from app.adapters.base import TrainDataProvider, TrainDataProviderError
from app.adapters.mock_provider import MockTrainDataProvider
from app.domain.models import RouteQuery, SeatPrice, TrainSegment, TransferPlan
from app.services.cache import SqliteOdCache
from app.services.route_search import (
    ParetoFrontier,
    PathSearchState,
    ProviderBackedRouteSearchEngine,
    deserialize_train_segments,
    serialize_train_segments,
)
from app.services.search_telemetry import SqliteSearchTelemetryRecorder


class CountingMockTrainDataProvider(MockTrainDataProvider):
    def __init__(self) -> None:
        super().__init__()
        self.search_count = 0

    async def search_segments(self, from_station, to_station, query):
        self.search_count += 1
        return await super().search_segments(from_station, to_station, query)


class EmptyFirstLegProvider(TrainDataProvider):
    name = "empty-first-leg"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def candidate_transfer_stations(self, query: RouteQuery) -> list[str]:
        return ["中转站"]

    async def search_segments(self, from_station: str, to_station: str, query: RouteQuery) -> list[TrainSegment]:
        self.calls.append((from_station, to_station))
        return []


class SlowEmptyTransferProvider(TrainDataProvider):
    name = "slow-empty-transfer"

    def __init__(self, candidates: int) -> None:
        self.candidates = candidates
        self.active_queries = 0
        self.max_active_queries = 0

    def candidate_transfer_stations(self, query: RouteQuery) -> list[str]:
        return [f"中转站{index}" for index in range(self.candidates)]

    async def search_segments(self, from_station: str, to_station: str, query: RouteQuery) -> list[TrainSegment]:
        self.active_queries += 1
        self.max_active_queries = max(self.max_active_queries, self.active_queries)
        await asyncio.sleep(0.01)
        self.active_queries -= 1
        return []


class SlowBudgetProvider(TrainDataProvider):
    name = "slow-budget"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, date]] = []

    def candidate_transfer_stations(self, query: RouteQuery) -> list[str]:
        return ["中转站"]

    async def search_segments(self, from_station: str, to_station: str, query: RouteQuery) -> list[TrainSegment]:
        self.calls.append((from_station, to_station, query.date))
        await asyncio.sleep(0.01)
        return []


class FailingTransferCandidateProvider(MockTrainDataProvider):
    def candidate_transfer_stations(self, query: RouteQuery) -> list[str]:
        return ["坏中转站", "南京南"]

    async def search_segments(self, from_station, to_station, query):
        if "坏中转站" in {from_station, to_station}:
            raise TrainDataProviderError("候选中转站请求失败")
        return await super().search_segments(from_station, to_station, query)


class ExpensiveFirstLegProvider(TrainDataProvider):
    name = "expensive-first-leg"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def candidate_transfer_stations(self, query: RouteQuery) -> list[str]:
        return ["中转站"]

    async def search_segments(self, from_station: str, to_station: str, query: RouteQuery) -> list[TrainSegment]:
        self.calls.append((from_station, to_station))
        if (from_station, to_station) == ("起点", "终点"):
            return [priced_segment("D1", from_station, to_station, query.date, time(8, 0), time(10, 0), "100")]
        if (from_station, to_station) == ("起点", "中转站"):
            return [priced_segment("D2", from_station, to_station, query.date, time(8, 0), time(9, 0), "150")]
        if (from_station, to_station) == ("中转站", "终点"):
            return [priced_segment("D3", from_station, to_station, query.date, time(10, 0), time(11, 0), "1")]
        return []


class TwoTransferCheapestProvider(TrainDataProvider):
    name = "two-transfer-cheapest"

    def candidate_transfer_stations(self, query: RouteQuery) -> list[str]:
        return ["中转一", "中转二"]

    async def search_segments(self, from_station: str, to_station: str, query: RouteQuery) -> list[TrainSegment]:
        segments = {
            ("起点", "终点"): [priced_segment("D1", from_station, to_station, query.date, time(8, 0), time(12, 0), "500")],
            ("起点", "中转一"): [priced_segment("D2", from_station, to_station, query.date, time(8, 0), time(9, 0), "80")],
            ("中转一", "终点"): [priced_segment("D3", from_station, to_station, query.date, time(10, 0), time(13, 0), "300")],
            ("中转一", "中转二"): [priced_segment("D4", from_station, to_station, query.date, time(10, 0), time(11, 0), "70")],
            ("中转二", "终点"): [priced_segment("D5", from_station, to_station, query.date, time(12, 0), time(14, 0), "60")],
        }
        return segments.get((from_station, to_station), [])


class DominatedStateProvider(TrainDataProvider):
    name = "dominated-state"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def candidate_transfer_stations(self, query: RouteQuery) -> list[str]:
        return ["中转站"]

    async def search_segments(self, from_station: str, to_station: str, query: RouteQuery) -> list[TrainSegment]:
        self.calls.append((from_station, to_station))
        if (from_station, to_station) == ("起点", "中转站"):
            return [
                priced_segment("D1", from_station, to_station, query.date, time(8, 0), time(9, 0), "50"),
                priced_segment("D2", from_station, to_station, query.date, time(8, 10), time(9, 10), "60"),
            ]
        if (from_station, to_station) == ("中转站", "终点"):
            return [priced_segment("D3", from_station, to_station, query.date, time(10, 0), time(11, 0), "50")]
        return []


class NonDominatedArrivalProvider(TrainDataProvider):
    name = "non-dominated-arrival"

    def candidate_transfer_stations(self, query: RouteQuery) -> list[str]:
        return ["中转站"]

    async def search_segments(self, from_station: str, to_station: str, query: RouteQuery) -> list[TrainSegment]:
        if (from_station, to_station) == ("起点", "中转站"):
            return [
                priced_segment("D1", from_station, to_station, query.date, time(8, 0), time(10, 0), "40"),
                priced_segment("D2", from_station, to_station, query.date, time(8, 10), time(9, 0), "60"),
            ]
        if (from_station, to_station) == ("中转站", "终点"):
            return [priced_segment("D3", from_station, to_station, query.date, time(9, 30), time(10, 30), "50")]
        return []


class CandidateOrderProvider(TrainDataProvider):
    name = "candidate-order"

    def __init__(self, candidates: list[str]) -> None:
        self.candidates = candidates

    def candidate_transfer_stations(self, query: RouteQuery) -> list[str]:
        return self.candidates

    async def search_segments(self, from_station: str, to_station: str, query: RouteQuery) -> list[TrainSegment]:
        return []


class ProgressiveCheaperTransferProvider(TrainDataProvider):
    name = "progressive-cheaper-transfer"

    def candidate_transfer_stations(self, query: RouteQuery) -> list[str]:
        return ["较贵中转", "便宜中转"]

    async def search_segments(self, from_station: str, to_station: str, query: RouteQuery) -> list[TrainSegment]:
        if "较贵中转" in {from_station, to_station}:
            await asyncio.sleep(0.01)
        if "便宜中转" in {from_station, to_station}:
            await asyncio.sleep(0.02)
        segments = {
            ("起点", "终点"): [priced_segment("D0", from_station, to_station, query.date, time(8, 0), time(12, 0), "300")],
            ("起点", "较贵中转"): [priced_segment("D1", from_station, to_station, query.date, time(8, 0), time(9, 0), "80")],
            ("较贵中转", "终点"): [priced_segment("D2", from_station, to_station, query.date, time(10, 0), time(11, 0), "80")],
            ("起点", "便宜中转"): [priced_segment("D3", from_station, to_station, query.date, time(8, 0), time(9, 0), "50")],
            ("便宜中转", "终点"): [priced_segment("D4", from_station, to_station, query.date, time(10, 0), time(11, 0), "50")],
        }
        return segments.get((from_station, to_station), [])


class MultipleSeatPriceProvider(TrainDataProvider):
    name = "multiple-seat-price"

    def candidate_transfer_stations(self, query: RouteQuery) -> list[str]:
        return []

    async def search_segments(self, from_station: str, to_station: str, query: RouteQuery) -> list[TrainSegment]:
        return [
            TrainSegment(
                train_no="D1",
                from_station=from_station,
                to_station=to_station,
                depart_at=datetime.combine(query.date, time(8, 0), tzinfo=timezone.utc),
                arrive_at=datetime.combine(query.date, time(9, 0), tzinfo=timezone.utc),
                duration_minutes=60,
                prices=[
                    SeatPrice(seat_type="一等座", price=Decimal("89"), remaining="有票"),
                    SeatPrice(seat_type="二等座", price=Decimal("56"), remaining="有票"),
                ],
                source=self.name,
                updated_at=datetime.now(timezone.utc),
            )
        ]


class ManyDirectPlansProvider(TrainDataProvider):
    name = "many-direct-plans"

    def candidate_transfer_stations(self, query: RouteQuery) -> list[str]:
        return []

    async def search_segments(self, from_station: str, to_station: str, query: RouteQuery) -> list[TrainSegment]:
        return [
            priced_segment(
                f"D{index}",
                from_station,
                to_station,
                query.date,
                time(6 + index, 0),
                time(7 + index, 0),
                str(index + 1),
            )
            for index in range(12)
        ]


class LongDurationDirectProvider(TrainDataProvider):
    name = "long-duration-direct"

    def candidate_transfer_stations(self, query: RouteQuery) -> list[str]:
        return []

    async def search_segments(self, from_station: str, to_station: str, query: RouteQuery) -> list[TrainSegment]:
        depart_at = datetime.combine(query.date, time(8, 0), tzinfo=timezone.utc)
        arrive_at = depart_at + timedelta(days=4)
        return [
            TrainSegment(
                train_no="K1",
                from_station=from_station,
                to_station=to_station,
                depart_at=depart_at,
                arrive_at=arrive_at,
                duration_minutes=int((arrive_at - depart_at).total_seconds() // 60),
                prices=[SeatPrice(seat_type="硬座", price=Decimal("100"), remaining="有票")],
                source=self.name,
                updated_at=datetime.now(timezone.utc),
            )
        ]


class EqualPriceComfortProvider(TrainDataProvider):
    name = "equal-price-comfort"

    def candidate_transfer_stations(self, query: RouteQuery) -> list[str]:
        return ["短等待", "舒适等待", "长等待"]

    async def search_segments(self, from_station: str, to_station: str, query: RouteQuery) -> list[TrainSegment]:
        if query.date != date(2026, 7, 1):
            return []
        segments = {
            ("起点", "短等待"): [priced_segment("D1", from_station, to_station, query.date, time(8, 0), time(9, 0), "50")],
            ("短等待", "终点"): [priced_segment("D2", from_station, to_station, query.date, time(9, 20), time(10, 20), "50")],
            ("起点", "舒适等待"): [priced_segment("D3", from_station, to_station, query.date, time(8, 0), time(9, 0), "50")],
            ("舒适等待", "终点"): [priced_segment("D4", from_station, to_station, query.date, time(10, 0), time(11, 0), "50")],
            ("起点", "长等待"): [priced_segment("D5", from_station, to_station, query.date, time(8, 0), time(9, 0), "50")],
            ("长等待", "终点"): [priced_segment("D6", from_station, to_station, query.date, time(12, 0), time(13, 0), "50")],
        }
        return segments.get((from_station, to_station), [])


def priced_segment(
    train_no: str,
    from_station: str,
    to_station: str,
    travel_date: date,
    depart: time,
    arrive: time,
    price: str,
) -> TrainSegment:
    depart_at = datetime.combine(travel_date, depart, tzinfo=timezone.utc)
    arrive_at = datetime.combine(travel_date, arrive, tzinfo=timezone.utc)
    return TrainSegment(
        train_no=train_no,
        from_station=from_station,
        to_station=to_station,
        depart_at=depart_at,
        arrive_at=arrive_at,
        duration_minutes=int((arrive_at - depart_at).total_seconds() // 60),
        prices=[SeatPrice(seat_type="硬座", price=Decimal(price), remaining="有票")],
        source="test",
        updated_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_stream_snapshots_replace_previous_top_plans() -> None:
    service = ProviderBackedRouteSearchEngine(ProgressiveCheaperTransferProvider(), remote_query_interval_seconds=0)
    query = RouteQuery(from_station="起点", to_station="终点", date=date(2026, 7, 1))

    snapshots = [snapshot async for snapshot in service.stream_snapshots(query)]

    assert len(snapshots) >= 2
    assert all(snapshot["type"] == "snapshot" for snapshot in snapshots)
    assert snapshots[0]["plans"][0].total_price == Decimal("300")
    assert snapshots[-1]["plans"][0].total_price == Decimal("100")
    assert snapshots[-1]["plans"][0].transfer_stations == ["便宜中转"]
    assert snapshots[-1].get("final") is True
    assert snapshots[-1]["pending_count"] == 0


def test_provider_backed_route_search_engine_exposes_engine_status() -> None:
    provider = MockTrainDataProvider()
    service = ProviderBackedRouteSearchEngine(provider)

    assert service.source == "mock"
    assert service.status["search_engine"] == "candidate"
    assert service.status["provider"] == "mock"


@pytest.mark.asyncio
async def test_search_returns_lowest_price_first() -> None:
    service = ProviderBackedRouteSearchEngine(MockTrainDataProvider())
    query = RouteQuery(from_station="北京", to_station="上海", date=date(2026, 7, 1))

    response = await service.search(query)

    assert response.plans
    assert response.plans[0].total_price <= response.plans[1].total_price
    assert response.plans[0].transfer_stations == ["南京南"]


@pytest.mark.asyncio
async def test_total_price_uses_lowest_seat_price_when_first_price_is_not_lowest() -> None:
    service = ProviderBackedRouteSearchEngine(MultipleSeatPriceProvider())
    query = RouteQuery(from_station="起点", to_station="终点", date=date(2026, 7, 1), max_transfers=0)

    response = await service.search(query)

    assert response.plans[0].total_price == Decimal("56")
    assert response.plans[0].segments[0].prices[0].price == Decimal("89")
    assert response.plans[0].segments[0].lowest_price == Decimal("56")


@pytest.mark.asyncio
async def test_search_returns_at_most_ten_plans() -> None:
    service = ProviderBackedRouteSearchEngine(ManyDirectPlansProvider())
    query = RouteQuery(from_station="起点", to_station="终点", date=date(2026, 7, 1), max_transfers=0)

    response = await service.search(query)

    assert len(response.plans) == 10
    assert [plan.total_price for plan in response.plans] == [Decimal(str(index)) for index in range(1, 11)]


@pytest.mark.asyncio
async def test_default_search_does_not_filter_by_total_duration() -> None:
    service = ProviderBackedRouteSearchEngine(LongDurationDirectProvider())
    query = RouteQuery(from_station="起点", to_station="终点", date=date(2026, 7, 1), max_transfers=0)

    response = await service.search(query)

    assert len(response.plans) == 1
    assert response.plans[0].total_duration_minutes == 4 * 24 * 60


@pytest.mark.asyncio
async def test_explicit_total_duration_limit_still_filters_plans() -> None:
    service = ProviderBackedRouteSearchEngine(LongDurationDirectProvider())
    query = RouteQuery(
        from_station="起点",
        to_station="终点",
        date=date(2026, 7, 1),
        max_transfers=0,
        max_total_duration_minutes=72 * 60,
    )

    response = await service.search(query)

    assert response.plans == []


@pytest.mark.asyncio
async def test_equal_price_transfer_plans_are_sorted_by_wait_comfort() -> None:
    service = ProviderBackedRouteSearchEngine(EqualPriceComfortProvider())
    query = RouteQuery(
        from_station="起点",
        to_station="终点",
        date=date(2026, 7, 1),
        min_transfer_minutes=10,
    )

    response = await service.search(query)

    assert [plan.transfer_stations for plan in response.plans] == [["舒适等待"], ["长等待"], ["短等待"]]
    assert [plan.total_price for plan in response.plans] == [Decimal("100"), Decimal("100"), Decimal("100")]


@pytest.mark.asyncio
async def test_min_transfer_filter_removes_too_short_transfer() -> None:
    service = ProviderBackedRouteSearchEngine(MockTrainDataProvider())
    query = RouteQuery(
        from_station="北京",
        to_station="上海",
        date=date(2026, 7, 1),
        min_transfer_minutes=40,
        max_total_duration_minutes=12 * 60,
    )

    response = await service.search(query)

    transfer_stations = [plan.transfer_stations for plan in response.plans]
    assert ["天津南"] not in transfer_stations
    assert ["南京南"] in transfer_stations


@pytest.mark.asyncio
async def test_one_transfer_skips_second_leg_when_first_leg_is_empty() -> None:
    provider = EmptyFirstLegProvider()
    service = ProviderBackedRouteSearchEngine(provider)
    query = RouteQuery(from_station="起点", to_station="终点", date=date(2026, 7, 1))

    await service.search(query)

    assert ("中转站", "终点") not in provider.calls


@pytest.mark.asyncio
async def test_search_limits_concurrent_remote_queries() -> None:
    provider = SlowEmptyTransferProvider(candidates=5)
    service = ProviderBackedRouteSearchEngine(provider, max_concurrent_remote_queries=2, remote_query_interval_seconds=0)
    query = RouteQuery(from_station="起点", to_station="终点", date=date(2026, 7, 1))

    await service.search(query)

    assert provider.max_active_queries == 2


@pytest.mark.asyncio
async def test_one_transfer_skips_failed_candidate_station() -> None:
    service = ProviderBackedRouteSearchEngine(FailingTransferCandidateProvider())
    query = RouteQuery(from_station="北京", to_station="上海", date=date(2026, 7, 1))

    response = await service.search(query)

    assert ["南京南"] in [plan.transfer_stations for plan in response.plans]


@pytest.mark.asyncio
async def test_best_price_prunes_second_leg_when_first_leg_cannot_improve() -> None:
    provider = ExpensiveFirstLegProvider()
    service = ProviderBackedRouteSearchEngine(provider)
    query = RouteQuery(from_station="起点", to_station="终点", date=date(2026, 7, 1))

    response = await service.search(query)

    assert response.plans[0].total_price == Decimal("100")
    assert ("中转站", "终点") not in provider.calls


@pytest.mark.asyncio
async def test_a_star_finds_cheapest_two_transfer_route() -> None:
    service = ProviderBackedRouteSearchEngine(TwoTransferCheapestProvider())
    query = RouteQuery(from_station="起点", to_station="终点", date=date(2026, 7, 1), max_transfers=2)

    response = await service.search(query)

    assert response.plans[0].total_price == Decimal("210")
    assert response.plans[0].transfer_stations == ["中转一", "中转二"]


@pytest.mark.asyncio
async def test_a_star_respects_max_transfers_limit() -> None:
    service = ProviderBackedRouteSearchEngine(TwoTransferCheapestProvider())
    query = RouteQuery(from_station="起点", to_station="终点", date=date(2026, 7, 1), max_transfers=1)

    response = await service.search(query)

    assert response.plans[0].total_price == Decimal("380")
    assert response.plans[0].transfer_stations == ["中转一"]


@pytest.mark.asyncio
async def test_a_star_pareto_frontier_prunes_dominated_state() -> None:
    provider = DominatedStateProvider()
    service = ProviderBackedRouteSearchEngine(provider)
    query = RouteQuery(from_station="起点", to_station="终点", date=date(2026, 7, 1), max_transfers=2)

    response = await service.search(query)

    assert response.plans[0].total_price == Decimal("100")


def test_pareto_frontier_rejects_state_with_higher_price_and_later_arrival() -> None:
    frontier = ParetoFrontier()
    earlier = path_state("中转站", Decimal("50"), datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc))
    later = path_state("中转站", Decimal("60"), datetime(2026, 7, 1, 9, 10, tzinfo=timezone.utc))

    assert frontier.add_if_not_dominated(earlier) is True
    assert frontier.add_if_not_dominated(later) is False


@pytest.mark.asyncio
async def test_a_star_pareto_frontier_keeps_earlier_arrival_when_more_expensive() -> None:
    service = ProviderBackedRouteSearchEngine(NonDominatedArrivalProvider())
    query = RouteQuery(
        from_station="起点",
        to_station="终点",
        date=date(2026, 7, 1),
        max_transfers=2,
        max_total_duration_minutes=12 * 60,
    )

    response = await service.search(query)

    assert response.plans[0].total_price == Decimal("110")
    assert response.plans[0].segments[0].train_no == "D2"


def test_pareto_frontier_keeps_state_with_earlier_arrival_even_if_more_expensive() -> None:
    frontier = ParetoFrontier()
    cheaper_late = path_state("中转站", Decimal("40"), datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc))
    expensive_early = path_state("中转站", Decimal("60"), datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc))

    assert frontier.add_if_not_dominated(cheaper_late) is True
    assert frontier.add_if_not_dominated(expensive_early) is True


@pytest.mark.asyncio
async def test_no_route_returns_empty_list() -> None:
    service = ProviderBackedRouteSearchEngine(MockTrainDataProvider())
    query = RouteQuery(from_station="杭州东", to_station="北京", date=date(2026, 7, 1))

    response = await service.search(query)

    assert response.plans == []


@pytest.mark.asyncio
async def test_search_uses_segment_cache_for_repeated_query() -> None:
    provider = CountingMockTrainDataProvider()
    service = ProviderBackedRouteSearchEngine(provider)
    query = RouteQuery(from_station="北京", to_station="上海", date=date(2026, 7, 1))

    await service.search(query)
    first_search_count = provider.search_count
    await service.search(query)

    assert first_search_count > 0
    assert provider.search_count == first_search_count


@pytest.mark.asyncio
async def test_search_uses_persistent_segment_cache_across_service_instances(tmp_path) -> None:
    cache_path = tmp_path / "segments.sqlite"
    persistent_cache = SqliteOdCache[list[TrainSegment]](
        cache_path,
        ttl_seconds=60,
        serializer=serialize_train_segments,
        deserializer=deserialize_train_segments,
    )
    seed_provider = CountingMockTrainDataProvider()
    seed_service = ProviderBackedRouteSearchEngine(seed_provider, persistent_cache=persistent_cache)
    query = RouteQuery(from_station="北京", to_station="上海", date=date(2026, 7, 1), max_transfers=0)

    await seed_service.search(query)
    cached_provider = CountingMockTrainDataProvider()
    cached_service = ProviderBackedRouteSearchEngine(cached_provider, persistent_cache=persistent_cache)
    response = await cached_service.search(query)

    assert response.plans
    assert seed_provider.search_count == 1
    assert cached_provider.search_count == 0
    assert cached_service.remote_query_count == 0


@pytest.mark.asyncio
async def test_search_records_telemetry_for_best_plan(tmp_path) -> None:
    telemetry = SqliteSearchTelemetryRecorder(tmp_path / "telemetry.sqlite")
    service = ProviderBackedRouteSearchEngine(MockTrainDataProvider(), telemetry_recorder=telemetry)
    query = RouteQuery(from_station="北京", to_station="上海", date=date(2026, 7, 1))

    response = await service.search(query)

    assert response.plans[0].transfer_stations == ["南京南"]
    assert telemetry.best_transfer_stations("mock", "北京", "上海") == ["南京南"]


@pytest.mark.asyncio
async def test_stream_records_telemetry_for_best_plan(tmp_path) -> None:
    telemetry = SqliteSearchTelemetryRecorder(tmp_path / "telemetry.sqlite")
    service = ProviderBackedRouteSearchEngine(MockTrainDataProvider(), telemetry_recorder=telemetry)
    query = RouteQuery(from_station="北京", to_station="上海", date=date(2026, 7, 1))

    plans = [plan async for plan in service.stream(query)]

    assert plans
    assert telemetry.best_transfer_stations("mock", "北京", "上海") == ["南京南"]


@pytest.mark.asyncio
async def test_search_telemetry_accumulates_transfer_station_hits(tmp_path) -> None:
    telemetry = SqliteSearchTelemetryRecorder(tmp_path / "telemetry.sqlite")
    query = RouteQuery(from_station="北京", to_station="上海", date=date(2026, 7, 1))

    await ProviderBackedRouteSearchEngine(MockTrainDataProvider(), telemetry_recorder=telemetry).search(query)
    await ProviderBackedRouteSearchEngine(MockTrainDataProvider(), telemetry_recorder=telemetry).search(query)

    assert telemetry.best_transfer_stations("mock", "北京", "上海", limit=1) == ["南京南"]


def test_a_star_ranked_candidates_merges_telemetry_station_not_in_provider(tmp_path) -> None:
    telemetry = SqliteSearchTelemetryRecorder(tmp_path / "telemetry.sqlite")
    provider = CandidateOrderProvider(["静态一", "静态二"])
    query = RouteQuery(from_station="起点", to_station="终点", date=date(2026, 7, 1), max_transfers=2)
    telemetry.record_search(
        query=query,
        provider=provider.name,
        plans=[telemetry_plan("历史站", "10")],
        remote_query_count=1,
    )
    service = ProviderBackedRouteSearchEngine(provider, telemetry_recorder=telemetry)

    candidates = service.cheapest_path_searcher._ranked_transfer_candidates(query)

    assert candidates == ["历史站", "静态一", "静态二"]


def test_a_star_ranked_candidates_prefers_higher_telemetry_hit_count(tmp_path) -> None:
    telemetry = SqliteSearchTelemetryRecorder(tmp_path / "telemetry.sqlite")
    provider = CandidateOrderProvider(["低命中", "高命中"])
    query = RouteQuery(from_station="起点", to_station="终点", date=date(2026, 7, 1), max_transfers=2)
    telemetry.record_search(query, provider.name, [telemetry_plan("低命中", "1")], remote_query_count=1)
    telemetry.record_search(query, provider.name, [telemetry_plan("高命中", "200")], remote_query_count=1)
    telemetry.record_search(query, provider.name, [telemetry_plan("高命中", "200")], remote_query_count=1)
    service = ProviderBackedRouteSearchEngine(provider, telemetry_recorder=telemetry)

    candidates = service.cheapest_path_searcher._ranked_transfer_candidates(query)

    assert candidates[:2] == ["高命中", "低命中"]


def test_one_transfer_ranked_candidates_also_use_telemetry(tmp_path) -> None:
    telemetry = SqliteSearchTelemetryRecorder(tmp_path / "telemetry.sqlite")
    provider = CandidateOrderProvider(["静态一", "静态二"])
    query = RouteQuery(from_station="起点", to_station="终点", date=date(2026, 7, 1), max_transfers=1)
    telemetry.record_search(
        query=query,
        provider=provider.name,
        plans=[telemetry_plan("历史站", "10")],
        remote_query_count=1,
    )
    service = ProviderBackedRouteSearchEngine(provider, telemetry_recorder=telemetry)

    candidates = service._ranked_transfer_candidates(query)

    assert candidates == ["历史站", "静态一", "静态二"]


@pytest.mark.asyncio
async def test_search_stops_requesting_segments_after_budget_exhausted() -> None:
    provider = CountingMockTrainDataProvider()
    service = ProviderBackedRouteSearchEngine(provider, max_remote_queries=1)
    query = RouteQuery(from_station="北京", to_station="上海", date=date(2026, 7, 1))

    await service.search(query)

    assert provider.search_count == 1
    assert service.remote_query_count == 1


@pytest.mark.asyncio
async def test_concurrent_searches_have_independent_remote_query_budget() -> None:
    provider = SlowBudgetProvider()
    service = ProviderBackedRouteSearchEngine(provider, max_remote_queries=2, max_concurrent_remote_queries=10)
    query = RouteQuery(from_station="起点", to_station="终点", date=date(2026, 7, 1), max_transfers=1)

    await asyncio.gather(service.search(query), service.search(query))

    assert len(provider.calls) == 4
    assert service.remote_query_count == 2


def test_route_query_default_allows_long_normal_train_direct_routes() -> None:
    query = RouteQuery(from_station="厦门北", to_station="西安", date=date(2026, 6, 29))

    assert query.max_total_duration_minutes is None


def test_route_query_strips_station_names() -> None:
    query = RouteQuery(from_station=" 北京 ", to_station=" 上海 ", date=date(2026, 7, 1))

    assert query.from_station == "北京"
    assert query.to_station == "上海"


@pytest.mark.parametrize(
    "payload",
    [
        {"from_station": "   ", "to_station": "上海", "date": date(2026, 7, 1)},
        {"from_station": "北京", "to_station": "\t", "date": date(2026, 7, 1)},
        {"from_station": "北京", "to_station": "北京", "date": date(2026, 7, 1)},
        {"from_station": "北" * 65, "to_station": "上海", "date": date(2026, 7, 1)},
        {"from_station": "北京", "to_station": "上海", "date": date(2026, 7, 1), "max_total_duration_minutes": 59},
        {"from_station": "北京", "to_station": "上海", "date": date(2026, 7, 1), "candidate_limit": 0},
        {"from_station": "北京", "to_station": "上海", "date": date(2026, 7, 1), "candidate_limit": 201},
        {"from_station": "北京", "to_station": "上海", "date": date(2026, 7, 1), "candidate_strategy": "unknown"},
        {"from_station": "北京", "to_station": "上海", "date": date(2026, 7, 1), "max_detour_ratio": -1},
        {"from_station": "北京", "to_station": "上海", "date": date(2026, 7, 1), "corridor_width_km": 0},
    ],
)
def test_route_query_rejects_invalid_station_or_duration(payload) -> None:
    with pytest.raises(ValueError):
        RouteQuery(**payload)


def path_state(station: str, price: Decimal, arrive_at: datetime) -> PathSearchState:
    return PathSearchState(
        current_station=station,
        segments=(priced_segment("D0", "起点", station, arrive_at.date(), time(8, 0), arrive_at.time(), str(price)),),
        accumulated_price=price,
        arrive_at=arrive_at,
        visited_stations=frozenset({"起点", station}),
    )


def telemetry_plan(transfer_station: str, total_price: str) -> TransferPlan:
    return TransferPlan(
        total_price=Decimal(total_price),
        total_duration_minutes=1,
        transfer_minutes=0,
        transfer_stations=[transfer_station],
        segments=[],
    )
