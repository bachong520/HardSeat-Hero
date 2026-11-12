"""生成一份"手工高铁网络"的 SQLite 基础边库，用于在没有 12306 数据时跑通离线搜索。

这不是真实票价，是一份结构正确的迷你数据集：
    ROUTE_SEARCH_ENGINE=time-expanded
    TIME_EXPANDED_GRAPH_DB=data/demo_time_expanded.sqlite3

能跑什么：
    python scripts/seed_demo_offline_graph.py            # 建库 + 演示查询
    python scripts/seed_demo_offline_graph.py --no-demo  # 只建库
    python scripts/seed_demo_offline_graph.py --db D:/tmp/x.sqlite3 --max-transfers 2

设计要点：
    1. 数据写进 train_od_fare_edge 表（基础边），不是按日期展开的快照表。
    2. 每条边 = 一辆车的一段行程，带"几点发、几点到、几天后到"。
    3. 查询时由引擎按用户选的日期，把基础边展开成具体的日期时间。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from app.domain.models import RouteQuery
from app.services import time_expanded_route_search as time_expanded_module
from app.services.static_price_repository import SQLiteStaticPriceRepository, TrainOdFareEdge
from app.services.time_expanded_route_search import TimeExpandedRouteSearchEngine

# 引擎默认认这个 provider 名；换成别的名字，查库时就对不上了。
PROVIDER = "12306-public-price"

# 京广高铁沿线的站序，用来填 from_station_no / to_station_no（真实数据里是经停顺序）
STATION_ORDER = {
    "广州南": 1,
    "长沙南": 2,
    "武汉": 3,
    "郑州东": 4,
    "石家庄": 5,
    "北京西": 6,
}

# 每条：(车次, 出发站, 到达站, 发车时刻, 到达时刻, 二等座票价, 一等座票价或 None)
# 票价刻意设计成"直达贵、中转便宜"，这样才看得出搜索算法真的在比价。
DEMO_ROUTES: list[tuple[str, str, str, str, str, str, str | None]] = [
    ("G79", "广州南", "北京西", "08:00", "16:08", "862.0", "1380.0"),  # 直达
    ("G6001", "广州南", "长沙南", "07:10", "09:32", "314.0", None),
    ("G6003", "广州南", "长沙南", "12:05", "14:27", "314.0", None),
    ("G65", "长沙南", "北京西", "10:30", "16:20", "505.0", None),
    ("G501", "长沙南", "北京西", "11:40", "17:45", "528.0", None),
    ("G1001", "广州南", "武汉", "08:20", "12:10", "463.0", None),
    ("G403", "武汉", "北京西", "13:00", "17:30", "520.0", None),
    ("G1101", "武汉", "郑州东", "09:00", "11:00", "244.0", None),
    ("G801", "郑州东", "北京西", "11:40", "14:30", "309.0", None),
]


def _minutes_between(depart: time, arrive: time) -> tuple[int, int]:
    """返回 (运行分钟数, 是否跨天)。"""
    depart_minutes = depart.hour * 60 + depart.minute
    arrive_minutes = arrive.hour * 60 + arrive.minute
    if arrive_minutes >= depart_minutes:
        return arrive_minutes - depart_minutes, 0
    return arrive_minutes + 24 * 60 - depart_minutes, 1


def build_edges() -> list[TrainOdFareEdge]:
    """把 DEMO_ROUTES 翻译成引擎认识的基础边对象。"""
    fetched_at = datetime(2026, 11, 1, 0, 0)
    edges: list[TrainOdFareEdge] = []

    for train_code, from_station, to_station, depart_text, arrive_text, second_class, first_class in DEMO_ROUTES:
        depart_time = time.fromisoformat(depart_text)
        arrive_time = time.fromisoformat(arrive_text)
        duration_minutes, arrive_day_offset = _minutes_between(depart_time, arrive_time)

        # 同一趟车的一个区间可能有好几种席别，每种席别一行数据（价格不同）。
        for seat_type, price in (("二等座", second_class), ("一等座", first_class)):
            if price is None:
                continue
            edges.append(
                TrainOdFareEdge(
                    provider=PROVIDER,
                    train_no=train_code,
                    train_code=train_code,
                    from_station=from_station,
                    to_station=to_station,
                    from_station_no=STATION_ORDER[from_station],
                    to_station_no=STATION_ORDER[to_station],
                    depart_time=depart_time,
                    arrive_time=arrive_time,
                    depart_day_offset=0,
                    arrive_day_offset=arrive_day_offset,
                    duration_minutes=duration_minutes,
                    seat_type=seat_type,
                    price=Decimal(price),
                    source="demo-seed",
                    fetched_at=fetched_at,
                )
            )

    return edges


async def run_demo_query(db_path: Path, max_transfers: int, top: int = 6, max_wait_minutes: int | None = None) -> None:
    """用刚建好的库跑一次查询，确认整条链路是通的。"""
    if top > time_expanded_module.MAX_RETURNED_PLANS:
        # 引擎默认只返回 10 条；想看被截断的部分，就临时把这个上限调大。
        time_expanded_module.MAX_RETURNED_PLANS = top

    engine = TimeExpandedRouteSearchEngine(
        repository=SQLiteStaticPriceRepository(db_path),
        provider=PROVIDER,
    )
    query_options = {}
    if max_wait_minutes is not None:
        query_options["max_transfer_minutes"] = max_wait_minutes
    query = RouteQuery(
        from_station="广州南",
        to_station="北京西",
        date=date(2026, 11, 20),
        max_transfers=max_transfers,
        **query_options,
    )

    plans = []
    async for snapshot in engine.stream_snapshots(query):
        plans = snapshot["plans"]

    print()
    print(f"查询 {query.from_station} -> {query.to_station}  {query.date}  最多换乘 {max_transfers} 次")
    print("-" * 72)
    if not plans:
        print("没有找到方案。检查一下站名是否和基础边里的完全一致。")
        return

    print(f"引擎一共返回 {len(plans)} 个方案（是否被上限截断见下方说明）")
    print()

    for index, plan in enumerate(plans[:top], start=1):
        transfer_text = "、".join(plan.transfer_stations) or "直达"
        print(
            f"{index}. {plan.total_price}元  "
            f"{plan.total_duration_minutes // 60}小时{plan.total_duration_minutes % 60}分  "
            f"换乘 {len(plan.transfer_stations)} 次（{transfer_text}）"
        )
        for segment in plan.segments:
            print(
                f"     {segment.train_no:<6} {segment.from_station} "
                f"{segment.depart_at:%m-%d %H:%M} -> {segment.to_station} {segment.arrive_at:%m-%d %H:%M} "
                f"{segment.lowest_price}元"
            )


def main() -> None:
    default_db = Path(__file__).resolve().parents[2] / "data" / "demo_time_expanded.sqlite3"
    parser = argparse.ArgumentParser(description="生成离线搜索用的迷你基础边库。")
    parser.add_argument("--db", default=str(default_db), help="SQLite 文件路径")
    parser.add_argument("--no-demo", action="store_true", help="只建库，不跑演示查询")
    parser.add_argument("--max-transfers", type=int, default=1, help="演示查询的最大换乘次数")
    parser.add_argument("--top", type=int, default=6, help="演示查询打印前几个方案（大于 10 会临时放大引擎上限）")
    parser.add_argument("--max-wait", type=int, default=None, help="单次换乘的最长等待分钟数（默认用引擎的 720 分钟）")
    args = parser.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    edges = build_edges()
    repository = SQLiteStaticPriceRepository(db_path)
    repository.upsert_train_od_fare_edges(PROVIDER, edges)

    print(f"已写入 {len(edges)} 条基础边 -> {db_path}")

    if not args.no_demo:
        asyncio.run(run_demo_query(db_path, args.max_transfers, args.top, args.max_wait))


if __name__ == "__main__":
    main()
