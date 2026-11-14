from __future__ import annotations

import sqlite3
from datetime import time
from decimal import Decimal
from pathlib import Path

from app.services.static_price_repository import SQLiteStaticPriceRepository
from scripts.build_fare_edges_from_od_snapshot import load_rows, to_edge

PROVIDER = "static-price"
TARGET_PROVIDER = "12306-public-price"

# (travel_date, origin, destination, train_no, train_code, depart_at, arrive_at,
#  duration_minutes, seat_type, price)
SAMPLE_ROWS = [
    ("2026-07-01", "北京", "上海", "T109", "T109", "2026-07-01T20:04:00+00:00", "2026-07-02T11:02:00+00:00", 898, "硬座", "177.5"),
    ("2026-07-01", "北京", "上海", "G547", "G547", "2026-07-01T06:18:00+00:00", "2026-07-01T12:11:00+00:00", 353, "商务座", "2782"),
    # 同一天次、同一区间、同一席别的第二天数据，用来验证"较晚的日期覆盖较早的"
    ("2026-07-02", "北京", "上海", "G547", "G547", "2026-07-02T06:20:00+00:00", "2026-07-02T12:13:00+00:00", 353, "商务座", "2782"),
]


def prepare_source(db_path: Path) -> None:
    SQLiteStaticPriceRepository(db_path)  # 借用仓储建表
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            """
            INSERT INTO od_public_price_snapshot(
                provider, travel_date, origin, destination, train_no, train_code,
                depart_at, arrive_at, duration_minutes, seat_type, price, currency,
                source, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CNY', '12306-public-price',
                      '2026-06-26T13:17:51+00:00')
            """,
            [(PROVIDER, *row) for row in SAMPLE_ROWS],
        )


def test_load_rows_filters_by_travel_date(tmp_path: Path) -> None:
    source = tmp_path / "static.sqlite3"
    prepare_source(source)

    assert len(load_rows(source, PROVIDER, "2026-07-01", None)) == 2
    assert len(load_rows(source, PROVIDER, None, None)) == 3
    assert len(load_rows(source, PROVIDER, "2026-07-01", 1)) == 1


def test_to_edge_splits_clock_time_from_day_offset(tmp_path: Path) -> None:
    source = tmp_path / "static.sqlite3"
    prepare_source(source)
    rows = load_rows(source, PROVIDER, "2026-07-01", None)
    overnight = next(row for row in rows if row["train_no"] == "T109")

    edge = to_edge(overnight, TARGET_PROVIDER)

    assert edge is not None
    assert edge.from_station == "北京"
    assert edge.to_station == "上海"
    assert edge.depart_time == time(20, 4)
    assert edge.arrive_time == time(11, 2)
    # 20:04 出发是当天，次日 11:02 才到 —— 跨天必须体现在 day_offset 上，
    # 否则引擎会把 11:02 当成同一天的到达时刻。
    assert edge.depart_day_offset == 0
    assert edge.arrive_day_offset == 1
    assert edge.duration_minutes == 898
    assert edge.price == Decimal("177.5")
    assert edge.provider == TARGET_PROVIDER


def test_conversion_is_idempotent_and_keeps_the_latest_date(tmp_path: Path) -> None:
    source = tmp_path / "static.sqlite3"
    prepare_source(source)
    target = tmp_path / "graph.sqlite3"

    rows = load_rows(source, PROVIDER, None, None)
    edges = [edge for row in rows if (edge := to_edge(row, TARGET_PROVIDER)) is not None]

    repository = SQLiteStaticPriceRepository(target)
    repository.upsert_train_od_fare_edges(TARGET_PROVIDER, edges)
    repository.upsert_train_od_fare_edges(TARGET_PROVIDER, edges)  # 再跑一次不应重复

    stored = repository.query_train_od_fare_edges(TARGET_PROVIDER)
    # 三行数据只有两个主键：G547 商务座跨日期合并成一条
    assert len(stored) == 2

    g547 = next(edge for edge in stored if edge.train_code == "G547")
    assert g547.depart_time == time(6, 20)  # 07-02 的数据胜出
