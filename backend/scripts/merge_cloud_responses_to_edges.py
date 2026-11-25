"""把云端采集回来的响应摘要合并成离线搜索用的基础边（阶段 6 的离线合并）。

链路
----
    crawl_cloud_station_od_response.response_excerpt   （JSON 信封，见 providers.build_response_excerpt）
        → 每行 queryLeftNewDTO
        → TrainOdFareEdge
        → SQLiteStaticPriceRepository.upsert_train_od_fare_edges

为什么先读信封而不是裸数组
--------------------------
采集阶段存的摘要是 `{"total_rows": N, "truncated": bool, "rows": [...]}`。
`truncated=true` 意味着**这次合并用的数据是不完整的**：一辆车可能整趟都没进来。
脚本会把每个任务的完整性打印出来，避免"用截断数据生成的图看起来和完整数据一样"。

用法
----
    # 先看这个 job 能合并出多少条边（不落库）
    python scripts/merge_cloud_responses_to_edges.py --job-id 2

    # 真的写进图
    python scripts/merge_cloud_responses_to_edges.py --job-id 2 --write

    # 换一个目标库 / 换个 provider 名字
    python scripts/merge_cloud_responses_to_edges.py --job-id 2 --target-db ../data/graph_from_cloud.sqlite3 --write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.adapters.railway_12306_public_price import PRICE_FIELDS, parse_price
from app.domain.station_city import city_of_station, load_station_city_map  # noqa: E402
from app.services.static_price_repository import (  # noqa: E402
    SQLiteStaticPriceRepository,
    TrainOdFareEdge,
)

DEFAULT_TARGET_DB = BACKEND_ROOT.parent / "data" / "graph_from_cloud.sqlite3"
DEFAULT_TARGET_PROVIDER = "12306-public-price"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把云端响应摘要合并成 train_od_fare_edge（阶段 6 离线合并）。"
    )
    parser.add_argument("--job-id", type=int, required=True, help="要合并的 crawl_cloud_job.id")
    parser.add_argument("--dsn", default=None, help="中心库 DSN；默认取环境变量")
    parser.add_argument("--database-url-env", default="CLOUD_CRAWL_DATABASE_URL")
    parser.add_argument("--target-db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--target-provider", default=DEFAULT_TARGET_PROVIDER)
    parser.add_argument(
        "--granularity",
        choices=("city", "station"),
        default="city",
        help="节点粒度：city 用城市名（默认，便于按城市搜索），station 保留具体车站",
    )
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 条响应，用于试跑")
    parser.add_argument("--write", action="store_true", help="真的写入目标库；不加只预览")
    return parser.parse_args()


def load_responses(dsn: str, job_id: int, limit: int | None) -> list[dict[str, Any]]:
    sql = (
        "SELECT task_id, from_station, to_station, crawl_date, row_count, response_body, response_excerpt"
        " FROM crawl_cloud_station_od_response"
        " WHERE job_id = %s AND COALESCE(response_body, response_excerpt) IS NOT NULL"
        " ORDER BY task_id"
    )
    params: list[Any] = [job_id]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(sql, params).fetchall()
    return [
        {
            "task_id": row[0],
            "from_station": row[1],
            "to_station": row[2],
            "crawl_date": row[3],
            "row_count": row[4],
            "response_body": row[5],
            "response_excerpt": row[6],
        }
        for row in rows
    ]


def parse_excerpt(text: str) -> tuple[list[dict[str, Any]], int, bool]:
    """拆开响应摘要信封，返回 (保留的行, 原始总行数, 是否被截断)。"""
    payload = json.loads(text)
    if not isinstance(payload, dict) or "rows" not in payload:
        raise ValueError("响应摘要不是预期的信封格式")
    rows = payload["rows"]
    if not isinstance(rows, list):
        raise ValueError("响应摘要里的 rows 不是列表")
    return rows, int(payload.get("total_rows", len(rows))), bool(payload.get("truncated", False))


def _duration_minutes(lishi: Any) -> int | None:
    text = str(lishi or "").strip()
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]) * 60 + int(parts[1])


def _raw_hash(*parts: object) -> str:
    joined = "|".join(str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def dto_to_edges(
    dto: dict[str, Any],
    *,
    origin: str,
    destination: str,
    target_provider: str,
    fetched_at: datetime,
    granularity: str = "city",
    station_city: dict[str, str] | None = None,
) -> list[TrainOdFareEdge]:
    """一行 queryLeftNewDTO → 每种有票价的席别一条基础边。

    时刻拆成"钟点 + 第几天"：基础边不带日期，一趟车每天都是同一个钟点。

    节点粒度（`granularity`）：

    - `city`（默认）：车站先过 `station_city` 映射表变成城市名。
      任务是「北京→上海」，12306 返回的是「北京南→上海虹桥」；把车站当节点，
      用户搜「北京→上海」就只能命中恰好叫「北京」的那一趟车。
      `backend/app/data/station_metadata.csv` 有现成的 name→city 列。
    - `station`：保留响应里的车站名，用于按具体车站搜索
      （例如只想看「北京南」出发的车）。

    两种粒度用不同的 provider 名写进同一个库即可共存，因为 `train_od_fare_edge`
    的主键里带 provider。
    """
    depart_text = str(dto.get("start_time") or "").strip()
    arrive_text = str(dto.get("arrive_time") or "").strip()
    duration_minutes = _duration_minutes(dto.get("lishi"))
    if not depart_text or not arrive_text or duration_minutes is None:
        return []

    try:
        depart_time = time.fromisoformat(depart_text)
        arrive_time = time.fromisoformat(arrive_text)
    except ValueError:
        return []

    train_code = str(dto.get("station_train_code") or "").strip()
    train_no = str(dto.get("train_no") or train_code).strip()
    if not train_code:
        return []

    # day_difference：这趟车从本区间出发日算起，第几天到达（跨天车 = 1）
    try:
        arrive_day_offset = int(dto.get("day_difference") or 0)
    except (TypeError, ValueError):
        arrive_day_offset = 0

    dto_origin = str(dto.get("from_station_name") or origin).strip()
    dto_destination = str(dto.get("to_station_name") or destination).strip()

    if granularity == "station":
        from_node, to_node = dto_origin, dto_destination
    else:
        from_node = city_of_station(dto_origin, station_city) or city_of_station(origin, station_city) or dto_origin
        to_node = (
            city_of_station(dto_destination, station_city)
            or city_of_station(destination, station_city)
            or dto_destination
        )

    edges: list[TrainOdFareEdge] = []
    for field, seat_type in PRICE_FIELDS.items():
        price = parse_price(dto.get(field))
        if price is None:
            continue
        edges.append(
            TrainOdFareEdge(
                provider=target_provider,
                train_no=train_no,
                train_code=train_code,
                from_station=from_node,
                to_station=to_node,
                from_station_no=int(dto.get("from_station_no") or 0),
                to_station_no=int(dto.get("to_station_no") or 0),
                depart_time=depart_time,
                arrive_time=arrive_time,
                depart_day_offset=0,
                arrive_day_offset=arrive_day_offset,
                duration_minutes=duration_minutes,
                seat_type=seat_type,
                price=price,
                source="cloud-worker-excerpt",
                fetched_at=fetched_at,
                # 车站名进哈希，保留"这条边具体来自哪两个车站"的可追溯性
                raw_hash=_raw_hash(train_code, dto_origin, dto_destination, seat_type, price),
            )
        )
    return edges


def merge(
    responses: list[dict[str, Any]],
    target_provider: str,
    *,
    granularity: str = "city",
) -> tuple[list[TrainOdFareEdge], list[dict[str, Any]]]:
    edges: list[TrainOdFareEdge] = []
    report: list[dict[str, Any]] = []
    station_city = load_station_city_map()
    for response in responses:
        fetched_at = datetime.now(timezone.utc)
        # 优先用完整响应体；老数据只有摘要时退回摘要（那时 truncated 会是 true）
        body = response.get("response_body")
        payload = body or response["response_excerpt"]
        source = "body" if body else "excerpt"
        rows, total_rows, truncated = parse_excerpt(payload)
        task_edges: list[TrainOdFareEdge] = []
        unmapped: set[str] = set()
        for row in rows:
            dto = row.get("queryLeftNewDTO") if isinstance(row, dict) else None
            if not isinstance(dto, dict):
                continue
            for field in ("from_station_name", "to_station_name"):
                station = str(dto.get(field) or "").strip()
                if station and city_of_station(station, station_city) is None:
                    unmapped.add(station)
            task_edges.extend(
                dto_to_edges(
                    dto,
                    origin=response["from_station"],
                    destination=response["to_station"],
                    target_provider=target_provider,
                    fetched_at=fetched_at,
                    granularity=granularity,
                    station_city=station_city,
                )
            )
        edges.extend(task_edges)
        report.append(
            {
                "task_id": response["task_id"],
                "od": f"{response['from_station']}→{response['to_station']}",
                "crawl_date": response["crawl_date"],
                "rows_stored": len(rows),
                "rows_total": total_rows,
                "truncated": truncated,
                "payload_source": source,
                "edges": len(task_edges),
                "unmapped": sorted(unmapped),
            }
        )
    return edges, report


def print_report(report: list[dict[str, Any]], edges: list[TrainOdFareEdge], target_db: Path, written: bool) -> None:
    print(f"{'task':>5}  {'OD':<14} {'日期':<12} {'存下/总数':>10}  {'截断':<4} 边数")
    print("-" * 68)
    for item in report:
        crawl_date = item["crawl_date"]
        date_text = crawl_date.isoformat() if isinstance(crawl_date, date) else str(crawl_date)
        stored = f"{item['rows_stored']}/{item['rows_total']}"
        truncated = "是" if item["truncated"] else "否"
        print(
            f"{item['task_id']:>5}  {item['od']:<14} {date_text:<12} {stored:>10}  {truncated:<4} {item['edges']}"
        )
    print("-" * 68)
    print(f"响应数={len(report)}  基础边数={len(edges)}")

    truncated_tasks = [item for item in report if item["truncated"]]
    if truncated_tasks:
        print()
        print("注意：以下任务库里只有摘要、没有完整响应体，合并用的数据是被截断的：")
        for item in truncated_tasks:
            print(f"  task {item['task_id']}  {item['od']}  只有 {item['rows_stored']}/{item['rows_total']} 行")

    unmapped = sorted({name for item in report for name in item.get("unmapped", [])})
    if unmapped:
        print()
        print(f"注意：{len(unmapped)} 个车站不在 station_metadata.csv 里，节点回落成站名本身：")
        print("  " + "、".join(unmapped[:20]))
        if len(unmapped) > 20:
            print(f"  ……还有 {len(unmapped) - 20} 个")

    if written:
        print()
        print(f"已写入 {target_db}")


def main() -> int:
    args = parse_args()
    dsn = args.dsn or os.getenv(args.database_url_env)
    if not dsn:
        print(f"缺少数据库连接：请设置 {args.database_url_env} 或用 --dsn 传入", file=sys.stderr)
        return 2

    responses = load_responses(dsn, args.job_id, args.limit)
    if not responses:
        print(f"job {args.job_id} 没有可合并的响应（response_excerpt 全为空？）")
        return 1

    edges, report = merge(responses, args.target_provider, granularity=args.granularity)
    print_report(report, edges, args.target_db, written=args.write)

    if args.write and edges:
        repository = SQLiteStaticPriceRepository(args.target_db)
        repository.upsert_train_od_fare_edges(args.target_provider, edges)
        stored = repository.query_train_od_fare_edges(args.target_provider)
        print(f"图里现在有 {len(stored)} 条基础边")
    elif not args.write:
        print()
        print("这是预览。确认无误后加 --write 真正落库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
