from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.cloud_crawl.models import CloudStationOdTaskDraft
from app.cloud_crawl.postgres_repository import PostgresCloudCrawlRepository
from app.cloud_crawl.sample_tasks import (
    SampledStationOd,
    build_sampled_probe_task_drafts,
    read_sampled_station_ods,
    summarize_sampled_probe_drafts,
)


DEFAULT_PROVIDER = "12306-public-price"


class PocJobRepository(Protocol):
    def insert_job(
        self,
        *,
        provider: str,
        crawl_date: date,
        job_type: str = "public_price_station_od",
        task_mode: str = "sampled_probe",
        status: str = "pending",
    ) -> int: ...

    def insert_task(self, draft: CloudStationOdTaskDraft) -> object: ...


@dataclass(frozen=True, slots=True)
class PreparedPocJob:
    job_id: int
    task_count: int
    provider: str
    crawl_date: date
    task_mode: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a bounded real-provider cloud PoC job. Does not access 12306. Default is preview only."
    )
    parser.add_argument("--input-csv", required=True, help="CSV with origin/destination or from_station/to_station columns")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--crawl-date", type=date.fromisoformat, required=True)
    parser.add_argument("--max-tasks", type=int, default=1)
    parser.add_argument("--priority-start", type=int, default=100)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--job-status", choices=("pending", "running"), default="running")
    parser.add_argument("--database-url-env", default="CLOUD_CRAWL_DATABASE_URL")
    parser.add_argument("--initialize-schema", action="store_true")
    parser.add_argument("--write", action="store_true", help="Create job and insert tasks into PostgreSQL.")
    return parser.parse_args()


def validate_poc_job_args(args: argparse.Namespace) -> None:
    if args.provider != DEFAULT_PROVIDER:
        raise SystemExit(f"real PoC job provider must be {DEFAULT_PROVIDER}")
    if args.max_tasks < 1 or args.max_tasks > 3:
        raise SystemExit("real PoC job requires 1<=--max-tasks<=3")
    if args.max_attempts < 1 or args.max_attempts > 3:
        raise SystemExit("real PoC job requires 1<=--max-attempts<=3")


def build_preview_drafts(args: argparse.Namespace, samples: list[SampledStationOd]) -> list[CloudStationOdTaskDraft]:
    return build_sampled_probe_task_drafts(
        samples,
        job_id=0,
        provider=args.provider,
        crawl_date=args.crawl_date,
        max_tasks=args.max_tasks,
        priority_start=args.priority_start,
        max_attempts=args.max_attempts,
    )


def create_poc_job(
    repository: PocJobRepository,
    *,
    args: argparse.Namespace,
    samples: list[SampledStationOd],
) -> PreparedPocJob:
    job_id = repository.insert_job(
        provider=args.provider,
        crawl_date=args.crawl_date,
        job_type="public_price_station_od",
        task_mode="sampled_probe",
        status=args.job_status,
    )
    drafts = build_sampled_probe_task_drafts(
        samples,
        job_id=job_id,
        provider=args.provider,
        crawl_date=args.crawl_date,
        max_tasks=args.max_tasks,
        priority_start=args.priority_start,
        max_attempts=args.max_attempts,
    )
    for draft in drafts:
        repository.insert_task(draft)
    return PreparedPocJob(
        job_id=job_id,
        task_count=len(drafts),
        provider=args.provider,
        crawl_date=args.crawl_date,
        task_mode="sampled_probe",
    )


def main() -> int:
    args = parse_args()
    validate_poc_job_args(args)
    samples = read_sampled_station_ods(args.input_csv)
    preview_drafts = build_preview_drafts(args, samples)
    print(summarize_sampled_probe_drafts(preview_drafts))
    print(f"sampled_probe_task_count={len(preview_drafts)}")
    if not args.write:
        print("preview_only=true")
        return 0

    dsn = os.getenv(args.database_url_env)
    if not dsn:
        raise SystemExit(f"{args.database_url_env} is required when --write is set")
    repository = PostgresCloudCrawlRepository(dsn)
    if args.initialize_schema:
        repository.initialize_schema()
    prepared = create_poc_job(repository, args=args, samples=samples)
    print("preview_only=false")
    print(f"created_job_id={prepared.job_id}")
    print(f"inserted_task_count={prepared.task_count}")
    print(f"provider={prepared.provider}")
    print(f"crawl_date={prepared.crawl_date.isoformat()}")
    print(f"task_mode={prepared.task_mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
