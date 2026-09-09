from __future__ import annotations

import asyncio
import os
from dataclasses import asdict
from datetime import date
from typing import Any, Mapping, Protocol

from app.cloud_crawl.repository import CloudCrawlRepository, InMemoryCloudCrawlRepository
from app.cloud_crawl.worker import (
    CloudPriceProvider,
    CloudPriceWorkerConfig,
    MockCloudPriceProvider,
    run_cloud_price_worker,
)


class RemainingTimeContext(Protocol):
    def get_remaining_time_in_millis(self) -> int: ...


class CloudFunctionConfigError(ValueError):
    pass


def cloud_price_worker_handler(
    event: dict[str, Any] | None,
    context: RemainingTimeContext | None = None,
    *,
    environ: dict[str, str] | None = None,
    repository: CloudCrawlRepository | None = None,
    provider: CloudPriceProvider | None = None,
) -> dict[str, Any]:
    try:
        config = parse_worker_config(event or {}, environ=environ if environ is not None else os.environ)
    except CloudFunctionConfigError as exc:
        return {"ok": False, "error": {"type": "config_error", "message": str(exc)}}

    if remaining_time_too_low(context):
        return {
            "ok": True,
            "summary": {
                "job_id": config.job_id,
                "worker_id": config.worker_id,
                "claimed_count": 0,
                "processed_count": 0,
                "succeeded_count": 0,
                "skipped_count": 0,
                "retry_wait_count": 0,
                "dead_letter_count": 0,
                "timeout_count": 0,
                "p50_latency_ms": 0,
                "p95_latency_ms": 0,
                "errors": [],
                "stopped_reason": "remaining_time_too_low",
            },
        }

    summary = asyncio.run(
        run_cloud_price_worker(
            config,
            repository or create_repository(environ if environ is not None else os.environ),
            provider or MockCloudPriceProvider(),
        )
    )
    return {"ok": True, "summary": serialize_summary(summary)}


def create_repository(environ: Mapping[str, str]) -> CloudCrawlRepository:
    dsn = environ.get("CLOUD_CRAWL_DATABASE_URL")
    if not dsn:
        return InMemoryCloudCrawlRepository()

    from app.cloud_crawl.postgres_repository import PostgresCloudCrawlRepository

    return PostgresCloudCrawlRepository(dsn)


def parse_worker_config(
    event: dict[str, Any], *, environ: Mapping[str, str]
) -> CloudPriceWorkerConfig:
    job_id = parse_required_int(event, environ, "job_id", "CLOUD_CRAWL_JOB_ID")
    provider = parse_required_str(event, environ, "provider", "CLOUD_CRAWL_PROVIDER")
    crawl_date = parse_required_date(event, environ, "crawl_date", "CLOUD_CRAWL_DATE")
    worker_id = parse_optional_str(event, environ, "worker_id", "CLOUD_CRAWL_WORKER_ID", "cloud-worker")
    config = CloudPriceWorkerConfig(
        job_id=job_id,
        provider=provider,
        crawl_date=crawl_date,
        worker_id=worker_id,
        batch_size=parse_optional_int(event, environ, "batch_size", "CLOUD_CRAWL_DEFAULT_BATCH_SIZE", 20),
        max_tasks_per_invocation=parse_optional_int(
            event, environ, "max_tasks_per_invocation", "CLOUD_CRAWL_MAX_TASKS_PER_INVOCATION", 100
        ),
        single_instance_concurrency=parse_optional_int(
            event, environ, "single_instance_concurrency", "CLOUD_CRAWL_DEFAULT_CONCURRENCY", 1
        ),
        request_delay_ms=parse_optional_int(
            event, environ, "request_delay_ms", "CLOUD_CRAWL_DEFAULT_DELAY_MS", 0
        ),
        timeout_seconds=parse_optional_int(
            event, environ, "timeout_seconds", "CLOUD_CRAWL_TIMEOUT_SECONDS", 60
        ),
        lease_seconds=parse_optional_int(
            event, environ, "lease_seconds", "CLOUD_CRAWL_LEASE_SECONDS", 300
        ),
        dry_run=parse_optional_bool(event, environ, "dry_run", "CLOUD_CRAWL_DRY_RUN", False),
    )
    validate_worker_config(config)
    return config


def validate_worker_config(config: CloudPriceWorkerConfig) -> None:
    if config.batch_size <= 0:
        raise CloudFunctionConfigError("batch_size must be positive")
    if config.max_tasks_per_invocation < 0:
        raise CloudFunctionConfigError("max_tasks_per_invocation must be non-negative")
    if config.single_instance_concurrency <= 0:
        raise CloudFunctionConfigError("single_instance_concurrency must be positive")
    if config.request_delay_ms < 0:
        raise CloudFunctionConfigError("request_delay_ms must be non-negative")
    if config.timeout_seconds <= 0:
        raise CloudFunctionConfigError("timeout_seconds must be positive")
    if config.lease_seconds <= 0:
        raise CloudFunctionConfigError("lease_seconds must be positive")


def remaining_time_too_low(
    context: RemainingTimeContext | None, *, safety_margin_ms: int = 5000
) -> bool:
    if context is None or not hasattr(context, "get_remaining_time_in_millis"):
        return False
    return context.get_remaining_time_in_millis() <= safety_margin_ms


def serialize_summary(summary: Any) -> dict[str, Any]:
    data = asdict(summary)
    data["started_at"] = summary.started_at.isoformat()
    data["finished_at"] = summary.finished_at.isoformat() if summary.finished_at else None
    return data


def parse_required_str(
    event: dict[str, Any], environ: Mapping[str, str], event_key: str, env_key: str
) -> str:
    value = event.get(event_key, environ.get(env_key))
    if value is None or str(value).strip() == "":
        raise CloudFunctionConfigError(f"missing required field: {event_key}")
    return str(value)


def parse_optional_str(
    event: dict[str, Any], environ: Mapping[str, str], event_key: str, env_key: str, default: str
) -> str:
    value = event.get(event_key, environ.get(env_key, default))
    return str(value)


def parse_required_int(
    event: dict[str, Any], environ: Mapping[str, str], event_key: str, env_key: str
) -> int:
    value = parse_required_str(event, environ, event_key, env_key)
    return parse_int(value, event_key)


def parse_optional_int(
    event: dict[str, Any], environ: Mapping[str, str], event_key: str, env_key: str, default: int
) -> int:
    value = event.get(event_key, environ.get(env_key, default))
    return parse_int(value, event_key)


def parse_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CloudFunctionConfigError(f"{field_name} must be an integer") from exc


def parse_required_date(
    event: dict[str, Any], environ: Mapping[str, str], event_key: str, env_key: str
) -> date:
    value = parse_required_str(event, environ, event_key, env_key)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CloudFunctionConfigError(f"{event_key} must be an ISO date") from exc


def parse_optional_bool(
    event: dict[str, Any], environ: Mapping[str, str], event_key: str, env_key: str, default: bool
) -> bool:
    value = event.get(event_key, environ.get(env_key, default))
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise CloudFunctionConfigError(f"{event_key} must be a boolean")
