import sqlite3
from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest

from app.adapters.base import TrainDataProvider, TrainDataProviderError
from app.domain.models import RouteQuery, SeatPrice, TrainSegment
from app.services.static_price_repository import SQLiteStaticPriceRepository
from scripts.refresh_static_prices import OdPair, create_source_provider, read_od_csv, refresh_static_prices


class FakeRefreshProvider(TrainDataProvider):
    name = "fake-source"

    def __init__(self, failing_od: tuple[str, str] | None = None) -> None:
        self.failing_od = failing_od
        self.calls: list[tuple[str, str]] = []

    async def search_segments(self, from_station: str, to_station: str, query: RouteQuery) -> list[TrainSegment]:
        self.calls.append((from_station, to_station))
        if self.failing_od == (from_station, to_station):
            raise TrainDataProviderError("source failed")
        depart_at = datetime.combine(query.date, time(8, 0), tzinfo=timezone.utc)
        arrive_at = datetime.combine(query.date, time(10, 0), tzinfo=timezone.utc)
        return [
            TrainSegment(
                train_no="D1",
                from_station=from_station,
                to_station=to_station,
                depart_at=depart_at,
                arrive_at=arrive_at,
                duration_minutes=120,
                prices=[SeatPrice(seat_type="二等座", price=Decimal("309.0"))],
                source=self.name,
                updated_at=datetime(2026, 6, 26, 8, 0, tzinfo=timezone.utc),
            )
        ]

    def candidate_transfer_stations(self, query: RouteQuery) -> list[str]:
        return []


def test_read_od_csv_supports_origin_destination_columns(tmp_path) -> None:
    od_file = tmp_path / "od.csv"
    od_file.write_text("origin,destination\n北京,上海\n空,\n", encoding="utf-8")

    pairs = read_od_csv(od_file)

    assert pairs == [OdPair(origin="北京", destination="上海")]


def test_read_od_csv_reads_priority_and_reason_columns(tmp_path) -> None:
    od_file = tmp_path / "refresh_od.csv"
    od_file.write_text(
        "origin,destination,priority,reason\n北京,上海,20,popular_search\n",
        encoding="utf-8",
    )

    pairs = read_od_csv(od_file)

    assert pairs == [OdPair(origin="北京", destination="上海", priority=20, reason="popular_search")]


@pytest.mark.asyncio
async def test_refresh_static_prices_writes_segments_and_crawl_records(tmp_path) -> None:
    repository = SQLiteStaticPriceRepository(tmp_path / "static_prices.sqlite3")
    provider = FakeRefreshProvider()

    summary = await refresh_static_prices(
        repository=repository,
        source_provider=provider,
        travel_date=date(2026, 7, 1),
        od_pairs=[OdPair("北京", "上海")],
        interval_seconds=0,
    )

    assert summary.total_count == 1
    assert summary.success_count == 1
    assert summary.failed_count == 0
    assert provider.calls == [("北京", "上海")]
    segments = repository.query_segments("static-price", date(2026, 7, 1), "北京", "上海")
    assert len(segments) == 1
    assert segments[0].lowest_price == Decimal("309.0")
    with sqlite3.connect(repository.db_path) as connection:
        job = connection.execute("SELECT status, total_count, success_count, failed_count FROM crawl_job").fetchone()
        task = connection.execute("SELECT status, attempts, last_error FROM crawl_task").fetchone()
    assert job == ("success", 1, 1, 0)
    assert task == ("success", 1, None)


@pytest.mark.asyncio
async def test_refresh_static_prices_records_failure_and_continues(tmp_path) -> None:
    repository = SQLiteStaticPriceRepository(tmp_path / "static_prices.sqlite3")
    provider = FakeRefreshProvider(failing_od=("坏起点", "坏终点"))

    summary = await refresh_static_prices(
        repository=repository,
        source_provider=provider,
        travel_date=date(2026, 7, 1),
        od_pairs=[OdPair("坏起点", "坏终点"), OdPair("北京", "上海")],
        interval_seconds=0,
    )

    assert summary.total_count == 2
    assert summary.success_count == 1
    assert summary.failed_count == 1
    assert provider.calls == [("坏起点", "坏终点"), ("北京", "上海")]
    with sqlite3.connect(repository.db_path) as connection:
        job = connection.execute("SELECT status, success_count, failed_count FROM crawl_job").fetchone()
        tasks = connection.execute("SELECT origin, destination, status, last_error FROM crawl_task ORDER BY id").fetchall()
    assert job == ("partial_failed", 1, 1)
    assert tasks[0][0:3] == ("坏起点", "坏终点", "failed")
    assert "TrainDataProviderError: source failed" in tasks[0][3]
    assert tasks[1] == ("北京", "上海", "success", None)


@pytest.mark.asyncio
async def test_refresh_static_prices_respects_limit(tmp_path) -> None:
    repository = SQLiteStaticPriceRepository(tmp_path / "static_prices.sqlite3")
    provider = FakeRefreshProvider()

    summary = await refresh_static_prices(
        repository=repository,
        source_provider=provider,
        travel_date=date(2026, 7, 1),
        od_pairs=[OdPair("北京", "上海"), OdPair("天津", "上海")],
        interval_seconds=0,
        limit=1,
    )

    assert summary.total_count == 1
    assert provider.calls == [("北京", "上海")]


def test_create_source_provider_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported refresh source provider"):
        create_source_provider("unknown")