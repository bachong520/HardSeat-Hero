from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.domain.models import RouteQuery, SeatPrice, TrainSegment
from app.services.static_price_repository import SQLiteStaticPriceRepository, TrainOdFareEdge
from app.services.time_expanded_route_search import TimeExpandedRouteSearchEngine


async def final_response(engine: TimeExpandedRouteSearchEngine, query: RouteQuery):
    snapshots = [snapshot async for snapshot in engine.stream_snapshots(query)]
    assert snapshots
    final_snapshot = snapshots[-1]
    assert final_snapshot.get("final") is True
    return SimpleNamespace(
        source=final_snapshot["source"],
        plans=final_snapshot["plans"],
    )


def insert_od_row(
    repository: SQLiteStaticPriceRepository,
    *,
    service_date: date,
    train_no: str,
    from_station: str,
    to_station: str,
    depart_at: datetime,
    arrive_at: datetime,
    price: str,
    seat_type: str = "二等座",
) -> None:
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute(
            """
            INSERT INTO train_od_price_snapshot(
                provider, service_date, train_no, train_code,
                from_station, to_station, from_station_no, to_station_no,
                depart_at, arrive_at, duration_minutes, seat_type,
                price, currency, source, fetched_at, raw_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CNY', ?, ?, NULL)
            """,
            (
                "12306-public-price",
                service_date.isoformat(),
                train_no,
                train_no,
                from_station,
                to_station,
                1,
                2,
                depart_at.isoformat(),
                arrive_at.isoformat(),
                int((arrive_at - depart_at).total_seconds() // 60),
                seat_type,
                price,
                "fixture",
                datetime(2026, 6, 26, 8, 0, tzinfo=timezone.utc).isoformat(),
            ),
        )


def insert_fare_edge(
    repository: SQLiteStaticPriceRepository,
    *,
    train_no: str,
    from_station: str,
    to_station: str,
    depart_time: time,
    arrive_time: time,
    price: str,
    provider: str = "12306-public-price",
    depart_day_offset: int = 0,
    arrive_day_offset: int = 0,
    seat_type: str = "二等座",
) -> None:
    repository.upsert_train_od_fare_edges(
        provider,
        [
            TrainOdFareEdge(
                provider=provider,
                train_no=train_no,
                train_code=train_no,
                from_station=from_station,
                to_station=to_station,
                from_station_no=1,
                to_station_no=2,
                depart_time=depart_time,
                arrive_time=arrive_time,
                depart_day_offset=depart_day_offset,
                arrive_day_offset=arrive_day_offset,
                duration_minutes=120,
                seat_type=seat_type,
                price=Decimal(price),
                source="fixture-fare-edge",
                fetched_at=datetime(2026, 6, 26, 8, 0, tzinfo=timezone.utc),
            )
        ],
    )


@pytest.mark.asyncio
async def test_time_expanded_search_returns_direct_plan(tmp_path) -> None:
    repository = SQLiteStaticPriceRepository(tmp_path / "static_prices.sqlite3")
    insert_od_row(
        repository,
        service_date=date(2026, 7, 1),
        train_no="D1",
        from_station="北京",
        to_station="上海",
        depart_at=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        arrive_at=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        price="309.0",
    )

    engine = TimeExpandedRouteSearchEngine(repository)
    response = await final_response(engine, RouteQuery(from_station="北京", to_station="上海", date=date(2026, 7, 1), max_transfers=0))

    assert response.source == "time-expanded"
    assert len(response.plans) == 1
    assert response.plans[0].total_price == Decimal("309.0")
    assert len(response.plans[0].segments) == 1
    assert response.plans[0].segments[0].train_no == "D1"


@pytest.mark.asyncio
async def test_time_expanded_search_returns_one_transfer_plan(tmp_path) -> None:
    repository = SQLiteStaticPriceRepository(tmp_path / "static_prices.sqlite3")
    insert_od_row(
        repository,
        service_date=date(2026, 7, 1),
        train_no="D1",
        from_station="北京",
        to_station="中转站",
        depart_at=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        arrive_at=datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc),
        price="50",
    )
    insert_od_row(
        repository,
        service_date=date(2026, 7, 1),
        train_no="D2",
        from_station="中转站",
        to_station="上海",
        depart_at=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        arrive_at=datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc),
        price="60",
    )

    engine = TimeExpandedRouteSearchEngine(repository)
    response = await final_response(
        engine,
        RouteQuery(from_station="北京", to_station="上海", date=date(2026, 7, 1), max_transfers=1, min_transfer_minutes=30)
    )

    assert len(response.plans) == 1
    plan = response.plans[0]
    assert plan.total_price == Decimal("110")
    assert plan.transfer_stations == ["中转站"]
    assert plan.transfer_minutes == 60
    assert [segment.train_no for segment in plan.segments] == ["D1", "D2"]


@pytest.mark.asyncio
async def test_time_expanded_search_rejects_insufficient_transfer_time(tmp_path) -> None:
    repository = SQLiteStaticPriceRepository(tmp_path / "static_prices.sqlite3")
    insert_od_row(
        repository,
        service_date=date(2026, 7, 1),
        train_no="D1",
        from_station="北京",
        to_station="中转站",
        depart_at=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        arrive_at=datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc),
        price="50",
    )
    insert_od_row(
        repository,
        service_date=date(2026, 7, 1),
        train_no="D2",
        from_station="中转站",
        to_station="上海",
        depart_at=datetime(2026, 7, 1, 9, 15, tzinfo=timezone.utc),
        arrive_at=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        price="60",
    )

    engine = TimeExpandedRouteSearchEngine(repository)
    response = await final_response(
        engine,
        RouteQuery(from_station="北京", to_station="上海", date=date(2026, 7, 1), max_transfers=1, min_transfer_minutes=30)
    )

    assert response.plans == []


@pytest.mark.asyncio
async def test_time_expanded_search_does_not_require_provider_argument(tmp_path) -> None:
    repository = SQLiteStaticPriceRepository(tmp_path / "static_prices.sqlite3")
    insert_od_row(
        repository,
        service_date=date(2026, 7, 1),
        train_no="D1",
        from_station="北京",
        to_station="上海",
        depart_at=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        arrive_at=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        price="309.0",
    )

    engine = TimeExpandedRouteSearchEngine(repository)
    response = await final_response(engine, RouteQuery(from_station="北京", to_station="上海", date=date(2026, 7, 1)))

    assert response.plans[0].total_price == Decimal("309.0")


@pytest.mark.asyncio
async def test_time_expanded_search_reads_train_od_fare_edges(tmp_path) -> None:
    repository = SQLiteStaticPriceRepository(tmp_path / "static_prices.sqlite3")
    insert_fare_edge(
        repository,
        train_no="D1",
        from_station="北京",
        to_station="上海",
        depart_time=time(8, 0),
        arrive_time=time(10, 0),
        price="309.0",
    )

    engine = TimeExpandedRouteSearchEngine(repository)
    response = await final_response(engine, RouteQuery(from_station="北京", to_station="上海", date=date(2026, 7, 12), max_transfers=0))

    # 基础边会被展开成 7-12/7-13/7-14 三份，但首段只接受查询当天出发，
    # 所以这里只应回来 1 个方案。
    assert len(response.plans) == 1
    segment = response.plans[0].segments[0]
    assert segment.train_no == "D1"
    assert segment.depart_at == datetime(2026, 7, 12, 8, 0, tzinfo=timezone.utc)
    assert segment.arrive_at == datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc)
    assert segment.source == "fixture-fare-edge"


@pytest.mark.asyncio
async def test_time_expanded_search_expands_cross_day_fare_edge(tmp_path) -> None:
    repository = SQLiteStaticPriceRepository(tmp_path / "static_prices.sqlite3")
    insert_fare_edge(
        repository,
        train_no="K1",
        from_station="北京",
        to_station="广州",
        depart_time=time(22, 0),
        arrive_time=time(8, 0),
        depart_day_offset=0,
        arrive_day_offset=1,
        price="250.0",
    )

    engine = TimeExpandedRouteSearchEngine(repository)
    response = await final_response(engine, RouteQuery(from_station="北京", to_station="广州", date=date(2026, 7, 12), max_transfers=0))

    segment = response.plans[0].segments[0]
    assert segment.depart_at == datetime(2026, 7, 12, 22, 0, tzinfo=timezone.utc)
    assert segment.arrive_at == datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_time_expanded_search_ignores_first_leg_departing_after_query_date(tmp_path) -> None:
    """首段必须在查询当天出发。

    查 7-1 时，7-2 才发车的车次不能被当成"当天方案"返回；否则同一趟车会被
    复制成多天的方案占满返回名额，把直达和更多换乘形态挤出前 10。
    """
    repository = SQLiteStaticPriceRepository(tmp_path / "static_prices.sqlite3")
    insert_od_row(
        repository,
        service_date=date(2026, 7, 1),
        train_no="D1",
        from_station="北京",
        to_station="上海",
        depart_at=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        arrive_at=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        price="309.0",
    )
    insert_od_row(
        repository,
        service_date=date(2026, 7, 2),
        train_no="D2",
        from_station="北京",
        to_station="上海",
        depart_at=datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc),
        arrive_at=datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc),
        price="309.0",
    )

    engine = TimeExpandedRouteSearchEngine(repository)
    response = await final_response(
        engine,
        RouteQuery(from_station="北京", to_station="上海", date=date(2026, 7, 1), max_transfers=0),
    )

    assert [plan.segments[0].train_no for plan in response.plans] == ["D1"]


def build_direct_plan(engine: TimeExpandedRouteSearchEngine, train_no: str, arrive_hour: int):
    segment = TrainSegment(
        train_no=train_no,
        from_station="北京",
        to_station="上海",
        depart_at=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        arrive_at=datetime(2026, 7, 1, arrive_hour, 0, tzinfo=timezone.utc),
        duration_minutes=(arrive_hour - 8) * 60,
        prices=[SeatPrice(seat_type="二等座", price=Decimal("309.0"), remaining="unknown")],
        source="fixture",
        updated_at=datetime(2026, 6, 26, 8, 0, tzinfo=timezone.utc),
    )
    return engine._build_plan([segment])


def test_keep_best_plan_per_train_sequence(tmp_path) -> None:
    """同一组车次只保留最优的一条，避免同日变体占满返回名额。"""
    engine = TimeExpandedRouteSearchEngine(SQLiteStaticPriceRepository(tmp_path / "static_prices.sqlite3"))
    slow_d1 = build_direct_plan(engine, "D1", 20)
    fast_d1 = build_direct_plan(engine, "D1", 10)
    d2 = build_direct_plan(engine, "D2", 12)

    kept = engine._keep_best_plan_per_train_sequence([slow_d1, fast_d1, d2])

    assert sorted(plan.segments[0].train_no for plan in kept) == ["D1", "D2"]
    kept_d1 = next(plan for plan in kept if plan.segments[0].train_no == "D1")
    assert kept_d1.total_duration_minutes == 120