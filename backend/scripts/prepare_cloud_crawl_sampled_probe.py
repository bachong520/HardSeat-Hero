from __future__ import annotations

import argparse
import os
from datetime import date

from app.cloud_crawl.postgres_repository import PostgresCloudCrawlRepository
from app.cloud_crawl.sample_tasks import (
    build_sampled_probe_task_drafts,
    read_sampled_station_ods,
    summarize_sampled_probe_drafts,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a bounded sampled_probe task set for cloud crawl PoC. Does not access 12306."
    )
    parser.add_argument("--input-csv", required=True, help="CSV with origin/destination or from_station/to_station columns")
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--crawl-date", type=date.fromisoformat, required=True)
    parser.add_argument("--max-tasks", type=int, default=10)
    parser.add_argument("--priority-start", type=int, default=100)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--write", action="store_true", help="Insert tasks into CLOUD_CRAWL_DATABASE_URL. Default is preview only.")
    parser.add_argument("--initialize-schema", action="store_true", help="Initialize PostgreSQL schema before inserting tasks.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    samples = read_sampled_station_ods(args.input_csv)
    drafts = build_sampled_probe_task_drafts(
        samples,
        job_id=args.job_id,
        provider=args.provider,
        crawl_date=args.crawl_date,
        max_tasks=args.max_tasks,
        priority_start=args.priority_start,
        max_attempts=args.max_attempts,
    )

    print(summarize_sampled_probe_drafts(drafts))
    print(f"sampled_probe_task_count={len(drafts)}")

    if not args.write:
        print("preview_only=true")
        return

    dsn = os.getenv("CLOUD_CRAWL_DATABASE_URL")
    if not dsn:
        raise SystemExit("CLOUD_CRAWL_DATABASE_URL is required when --write is set")

    repository = PostgresCloudCrawlRepository(dsn)
    if args.initialize_schema:
        repository.initialize_schema()
    for draft in drafts:
        repository.insert_task(draft)
    print("preview_only=false")
    print(f"inserted_or_existing_count={len(drafts)}")


if __name__ == "__main__":
    main()