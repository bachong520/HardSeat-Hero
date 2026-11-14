"""把按 OD 抓下来的票价快照转成离线搜索用的基础边。

背景
----
`od_public_price_snapshot` 里每一行是"某天、某趟车、从 A 到 B、某种席别"的
完整信息：出发时刻、到达时刻、时长、票价都有。而离线搜索真正读的
`train_od_fare_edge` 要的字段和它几乎一模一样，差的是三样东西：

1. 时刻要拆成"钟点 + 第几天"，因为基础边不带日期（一趟车每天都是这个钟点）；
2. 站序号码（from_station_no / to_station_no）留空即可，搜索用不到；
3. provider 名字要换，否则引擎按 provider 查不到。

所以这一步是纯本地的搬运，不需要联网，也不需要经停站快照。

注意主键
--------
`train_od_fare_edge` 的主键是
(provider, train_code, from_station, to_station, seat_type)，
**不含日期**——它描述的是"这趟车每天跑这个区间"，不是"某一天的特例"。
因此转换时只能选一个 travel_date，否则不同日期的同名车次会互相覆盖，
最后留下来的是哪一天的数据就变成随机的了。

用法
----
    # 先看有哪些日期可选
    python scripts/build_fare_edges_from_od_snapshot.py --list-dates

    # 把 2026-07-01 那天的数据转成图，写到新文件里
    python scripts/build_fare_edges_from_od_snapshot.py --date 2026-07-01

    # 只转前 5000 行，先看看效果
    python scripts/build_fare_edges_from_od_snapshot.py --date 2026-07-01 --limit 5000
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.static_price_repository import (  # noqa: E402
    SQLiteStaticPriceRepository,
    TrainOdFareEdge,
)

DEFAULT_SOURCE_DB = BACKEND_ROOT.parent / "data" / "static_prices.sqlite3"
DEFAULT_TARGET_DB = BACKEND_ROOT.parent / "data" / "graph_from_od.sqlite3"


def list_dates(source_db: Path, source_provider: str) -> list[str]:
    with sqlite3.connect(source_db) as connection:
        rows = connection.execute(
            "SELECT travel_date, COUNT(*) FROM od_public_price_snapshot"
            " WHERE provider = ? GROUP BY travel_date ORDER BY travel_date",
            (source_provider,),
        ).fetchall()
    return [f"{travel_date}  {count} 行" for travel_date, count in rows]


def _raw_hash(*parts: object) -> str:
    joined = "|".join(str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def load_rows(
    source_db: Path,
    source_provider: str,
    travel_date: str | None,
    limit: int | None,
) -> list[sqlite3.Row]:
    """读源表。travel_date 为 None 时读全部日期（按日期升序，靠后的覆盖靠前的）。

    按日期升序很重要：主键不含日期，同一天车次在不同日期的行会互相覆盖，
    升序排列能让"较新的一天"最后落库，避免被旧数据盖掉。
    """
    sql = (
        "SELECT travel_date, train_no, train_code, origin, destination, depart_at, arrive_at,"
        " duration_minutes, seat_type, price, source, fetched_at"
        " FROM od_public_price_snapshot"
        " WHERE provider = ?"
    )
    params: list[object] = [source_provider]
    if travel_date is not None:
        sql += " AND travel_date = ?"
        params.append(travel_date)
    sql += " ORDER BY travel_date ASC, train_code, origin, destination, seat_type"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    with sqlite3.connect(source_db) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(sql, params).fetchall()


def to_edge(row: sqlite3.Row, target_provider: str) -> TrainOdFareEdge | None:
    """把一行 OD 快照转成一条基础边；时刻解析不了就返回 None。"""
    try:
        depart_at = datetime.fromisoformat(row["depart_at"])
        arrive_at = datetime.fromisoformat(row["arrive_at"])
        service_date = date.fromisoformat(row["travel_date"])
    except (TypeError, ValueError):
        return None

    # 时刻只保留钟点，第几天单独存，"跨天"这件事才能被表达出来。
    depart_day_offset = (depart_at.date() - service_date).days
    arrive_day_offset = (arrive_at.date() - service_date).days

    return TrainOdFareEdge(
        provider=target_provider,
        train_no=row["train_no"],
        train_code=row["train_code"],
        from_station=row["origin"],
        to_station=row["destination"],
        # 站序号码搜索用不到，没有经停站数据时留 0。
        from_station_no=0,
        to_station_no=0,
        depart_time=depart_at.time(),
        arrive_time=arrive_at.time(),
        depart_day_offset=depart_day_offset,
        arrive_day_offset=arrive_day_offset,
        duration_minutes=int(row["duration_minutes"]),
        seat_type=row["seat_type"],
        price=Decimal(row["price"]),
        source=f"od-snapshot:{row['source']}",
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
        raw_hash=_raw_hash(
            target_provider,
            row["train_code"],
            row["origin"],
            row["destination"],
            row["seat_type"],
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="把 OD 票价快照转成离线搜索的基础边。")
    parser.add_argument("--source-db", default=str(DEFAULT_SOURCE_DB), help="带 od_public_price_snapshot 的库")
    parser.add_argument("--target-db", default=str(DEFAULT_TARGET_DB), help="写基础边的库（默认与源库分开）")
    parser.add_argument("--source-provider", default="static-price", help="源表里的 provider")
    parser.add_argument("--target-provider", default="12306-public-price", help="写进基础边时的 provider")
    parser.add_argument("--date", help="只转这一天（travel_date 的 YYYY-MM-DD）")
    parser.add_argument(
        "--all-dates",
        action="store_true",
        help="合并所有日期（覆盖面最大；同名车次以较晚的日期为准）",
    )
    parser.add_argument("--limit", type=int, help="最多读多少行，用于试跑")
    parser.add_argument("--list-dates", action="store_true", help="列出可选的日期后退出")
    args = parser.parse_args()

    source_db = Path(args.source_db)
    if not source_db.exists():
        raise SystemExit(f"找不到源库：{source_db}")

    if args.list_dates:
        print("可选日期（日期  行数）：")
        for line in list_dates(source_db, args.source_provider):
            print("  ", line)
        return

    if not args.date and not args.all_dates:
        raise SystemExit("必须指定 --date 或 --all-dates（可先用 --list-dates 看有哪些）")

    travel_date = None if args.all_dates else args.date
    rows = load_rows(source_db, args.source_provider, travel_date, args.limit)
    if not rows:
        raise SystemExit(f"没有数据（--date={args.date}，--source-provider={args.source_provider}）")

    edges: list[TrainOdFareEdge] = []
    skipped = 0
    for row in rows:
        edge = to_edge(row, args.target_provider)
        if edge is None:
            skipped += 1
            continue
        edges.append(edge)

    target_db = Path(args.target_db)
    repository = SQLiteStaticPriceRepository(target_db)  # 会自动建表
    repository.upsert_train_od_fare_edges(args.target_provider, edges)

    trains = {edge.train_no for edge in edges}
    stations = {edge.from_station for edge in edges} | {edge.to_station for edge in edges}
    covered_dates = {row["travel_date"] for row in rows}
    print(f"读到 {len(rows)} 行（{len(covered_dates)} 个日期），转换 {len(edges)} 条边，跳过 {skipped} 行")
    print(f"覆盖 {len(trains)} 趟车、{len(stations)} 个站")
    print(f"目标库：{target_db}（provider={args.target_provider}）")
    print()
    print("下一步验证：")
    print(f"  ROUTE_SEARCH_ENGINE=time-expanded \\")
    print(f"  TIME_EXPANDED_GRAPH_DB={target_db} \\")
    print(f"  TIME_EXPANDED_GRAPH_PROVIDER={args.target_provider} \\")
    print("  python -m uvicorn app.main:app --port 8010")


if __name__ == "__main__":
    main()
