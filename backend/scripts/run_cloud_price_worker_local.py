from __future__ import annotations

import argparse
import asyncio
import os
from datetime import date

from app.cloud_crawl.models import CloudStationOdTaskDraft
from app.cloud_crawl.postgres_repository import PostgresCloudCrawlRepository
from app.cloud_crawl.providers import Railway12306CloudPriceProvider
from app.cloud_crawl.repository import CloudCrawlRepository, InMemoryCloudCrawlRepository
from app.cloud_crawl.worker import (
    CloudPriceProvider,
    CloudPriceWorkerConfig,
    MockCloudPriceProvider,
    run_cloud_price_worker,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local mock cloud price worker.")
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--provider", default="mock-cloud-price")
    parser.add_argument("--crawl-date", type=date.fromisoformat, required=True)
    parser.add_argument("--worker-id", default="local-worker-1")
    parser.add_argument(
        "--provider-kind",
        choices=("mock", "railway-12306"),
        default="mock",
        help="Provider implementation. railway-12306 requires --allow-real-provider and conservative limits.",
    )
    parser.add_argument("--mock-mode", default="fixed_rows")
    parser.add_argument(
        "--allow-real-provider",
        action="store_true",
        help="Explicitly allow the real 12306 provider. Without this flag no network provider is constructed.",
    )
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--max-tasks-per-invocation", type=int, default=10)
    parser.add_argument("--single-instance-concurrency", type=int, default=1)
    parser.add_argument("--request-delay-ms", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--repository",
        choices=("memory", "postgres"),
        default="memory",
        help="Repository backend. Use postgres to claim existing tasks from CLOUD_CRAWL_DATABASE_URL.",
    )
    parser.add_argument(
        "--database-url-env",
        default="CLOUD_CRAWL_DATABASE_URL",
        help="Environment variable containing PostgreSQL DSN when --repository=postgres.",
    )
    return parser.parse_args()


def build_provider(args: argparse.Namespace) -> CloudPriceProvider:
    if args.provider_kind == "mock":
        return MockCloudPriceProvider(args.mock_mode)

    validate_real_provider_gate(args)
    return Railway12306CloudPriceProvider(timeout_seconds=args.timeout_seconds)


def validate_real_provider_gate(args: argparse.Namespace) -> None:
    if not args.allow_real_provider:
        raise SystemExit("--allow-real-provider is required when --provider-kind=railway-12306")
    if args.single_instance_concurrency != 1:
        raise SystemExit("railway-12306 provider requires --single-instance-concurrency=1")
    if args.max_tasks_per_invocation > 3:
        raise SystemExit("railway-12306 provider requires --max-tasks-per-invocation<=3 for initial PoC")
    if args.request_delay_ms < 1000:
        raise SystemExit("railway-12306 provider requires --request-delay-ms>=1000")
    if args.timeout_seconds < 10 or args.timeout_seconds > 60:
        raise SystemExit("railway-12306 provider requires 10<=--timeout-seconds<=60")


def build_repository(args: argparse.Namespace) -> CloudCrawlRepository:
    if args.repository == "postgres":
        dsn = os.getenv(args.database_url_env)
        if not dsn:
            raise SystemExit(f"{args.database_url_env} is required when --repository=postgres")
        return PostgresCloudCrawlRepository(dsn)

    repository = InMemoryCloudCrawlRepository()
    for index in range(args.max_tasks_per_invocation):
        repository.insert_task(
            CloudStationOdTaskDraft(
                job_id=args.job_id,
                provider=args.provider,
                crawl_date=args.crawl_date,
                from_station=f"站{index}",
                to_station=f"站{index + 1}",
            )
        )
    return repository


async def main() -> None:
    args = parse_args()
    repository = build_repository(args)
    config = CloudPriceWorkerConfig(
        job_id=args.job_id,
        provider=args.provider,
        crawl_date=args.crawl_date,
        worker_id=args.worker_id,
        batch_size=args.batch_size,
        max_tasks_per_invocation=args.max_tasks_per_invocation,
        single_instance_concurrency=args.single_instance_concurrency,
        request_delay_ms=args.request_delay_ms,
        timeout_seconds=args.timeout_seconds,
        lease_seconds=args.lease_seconds,
        dry_run=args.dry_run,
    )
    summary = await run_cloud_price_worker(
        config,
        repository,
        build_provider(args),
    )
    print(summary)


if __name__ == "__main__":
    asyncio.run(main())