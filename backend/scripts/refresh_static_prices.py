from __future__ import annotations

import argparse
import asyncio
import csv
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.adapters.base import TrainDataProvider  # noqa: E402
from app.adapters.railway_12306_public_price import Railway12306PublicPriceProvider  # noqa: E402
from app.domain.models import RouteQuery  # noqa: E402
from app.services.static_price_repository import SQLiteStaticPriceRepository  # noqa: E402


STATIC_PROVIDER_NAMESPACE = "static-price"


@dataclass(frozen=True)
class OdPair:
    origin: str
    destination: str
    priority: int = 100
    reason: str = ""


@dataclass(frozen=True)
class RefreshSummary:
    total_count: int
    success_count: int
    failed_count: int
    job_id: int

DEFAULT_TASK_PRIORITY = 100


def _parse_priority(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_TASK_PRIORITY
    text = raw.strip()
    if not text:
        return DEFAULT_TASK_PRIORITY
    try:
        return int(text)
    except ValueError:
        return DEFAULT_TASK_PRIORITY

def read_od_csv(path: str | Path) -> list[OdPair]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None:
            return []
        pairs: list[OdPair] = []
        for row in reader:
            origin = (row.get("origin") or row.get("from_station") or row.get("from") or "").strip()
            destination = (row.get("destination") or row.get("to_station") or row.get("to") or "").strip()
            if origin and destination:
                pairs.append(
                    OdPair(
                        origin=origin,
                        destination=destination,
                        priority=_parse_priority(row.get("priority")),
                        reason=(row.get("reason") or "").strip(),
                    )
                )
        return pairs


async def refresh_static_prices(
    *,
    repository: SQLiteStaticPriceRepository,
    source_provider: TrainDataProvider,
    travel_date: date,
    od_pairs: list[OdPair],
    interval_seconds: float = 0,
    limit: int | None = None,
) -> RefreshSummary:
    ordered_pairs = sorted(od_pairs, key=lambda pair: pair.priority)
    selected_pairs = ordered_pairs[:limit] if limit is not None else ordered_pairs
    started_at = datetime.now(timezone.utc)
    job_id = _create_job(repository.db_path, source_provider.name, travel_date, len(selected_pairs), started_at)
    success_count = 0
    failed_count = 0

    for index, pair in enumerate(selected_pairs):
        task_id = _create_task(repository.db_path, job_id, source_provider.name, travel_date, pair)
        try:
            query = RouteQuery(from_station=pair.origin, to_station=pair.destination, date=travel_date, max_transfers=0)
            segments = await source_provider.search_segments(pair.origin, pair.destination, query)
            repository.upsert_segments(
                STATIC_PROVIDER_NAMESPACE,
                travel_date,
                pair.origin,
                pair.destination,
                segments,
            )
            _finish_task(repository.db_path, task_id, "success", None)
            success_count += 1
        except Exception as exc:  # noqa: BLE001 - script must record failure and continue remaining OD tasks.
            _finish_task(repository.db_path, task_id, "failed", f"{type(exc).__name__}: {exc}")
            failed_count += 1
        if interval_seconds > 0 and index < len(selected_pairs) - 1:
            await asyncio.sleep(interval_seconds)

    _finish_job(repository.db_path, job_id, success_count, failed_count, datetime.now(timezone.utc))
    return RefreshSummary(
        total_count=len(selected_pairs),
        success_count=success_count,
        failed_count=failed_count,
        job_id=job_id,
    )


def create_source_provider(name: str) -> TrainDataProvider:
    normalized = name.strip().lower()
    if normalized in {"12306", "12306-public-price", "railway_12306_public_price"}:
        return Railway12306PublicPriceProvider()
    raise ValueError(f"Unsupported refresh source provider: {name}")


def _create_job(db_path: Path, provider: str, travel_date: date, total_count: int, started_at: datetime) -> int:
    with sqlite3.connect(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO crawl_job(
                provider, job_type, travel_date, scope, status,
                total_count, success_count, failed_count, started_at
            ) VALUES (?, 'static_price_refresh', ?, 'od-file', 'running', ?, 0, 0, ?)
            """,
            (provider, travel_date.isoformat(), total_count, started_at.isoformat()),
        )
        return int(cursor.lastrowid)


def _finish_job(db_path: Path, job_id: int, success_count: int, failed_count: int, finished_at: datetime) -> None:
    status = "success" if failed_count == 0 else "partial_failed"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE crawl_job
            SET status = ?, success_count = ?, failed_count = ?, finished_at = ?
            WHERE id = ?
            """,
            (status, success_count, failed_count, finished_at.isoformat(), job_id),
        )


def _create_task(db_path: Path, job_id: int, provider: str, travel_date: date, pair: OdPair) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO crawl_task(
                job_id, provider, travel_date, origin, destination,
                priority, status, attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'running', 1, ?, ?)
            """,
            (job_id, provider, travel_date.isoformat(), pair.origin, pair.destination, pair.priority, now, now),
        )
        return int(cursor.lastrowid)


def _finish_task(db_path: Path, task_id: int, status: str, error: str | None) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE crawl_task
            SET status = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, error, datetime.now(timezone.utc).isoformat(), task_id),
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="刷新 SQLite 静态公布票价库。第一版只接受明确 OD CSV，不做全国全量。")
    parser.add_argument("--date", required=True, help="出行日期，格式 YYYY-MM-DD")
    parser.add_argument("--od-file", required=True, help="OD CSV 文件，列名支持 origin,destination 或 from_station,to_station")
    parser.add_argument("--db", required=True, help="SQLite 静态票价库路径")
    parser.add_argument("--source-provider", default="12306-public-price", help="刷新数据源，当前支持 12306-public-price")
    parser.add_argument("--interval-seconds", type=float, default=1.0, help="每个 OD 请求之间的间隔秒数")
    parser.add_argument("--limit", type=int, help="最多刷新多少个 OD，用于小批量验证")
    return parser.parse_args(argv)


async def async_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    travel_date = date.fromisoformat(args.date)
    od_pairs = read_od_csv(args.od_file)
    if not od_pairs:
        print("OD CSV 为空或缺少有效 origin/destination", file=sys.stderr)
        return 2
    repository = SQLiteStaticPriceRepository(args.db)
    source_provider = create_source_provider(args.source_provider)
    summary = await refresh_static_prices(
        repository=repository,
        source_provider=source_provider,
        travel_date=travel_date,
        od_pairs=od_pairs,
        interval_seconds=args.interval_seconds,
        limit=args.limit,
    )
    print(
        f"刷新完成：job_id={summary.job_id}, total={summary.total_count}, "
        f"success={summary.success_count}, failed={summary.failed_count}"
    )
    return 0 if summary.failed_count == 0 else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())