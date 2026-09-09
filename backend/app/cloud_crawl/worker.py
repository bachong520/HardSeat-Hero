from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from time import perf_counter
from typing import Protocol

from app.cloud_crawl.metrics import percentile_latency_ms
from app.cloud_crawl.models import (
    ClaimedCloudStationOdTask,
    CloudStationOdTask,
    CloudTaskError,
    CloudTaskResponseSummary,
)
from app.cloud_crawl.repository import CloudCrawlRepository


class MockProviderMode(StrEnum):
    FIXED_ROWS = "fixed_rows"
    EMPTY_ROWS = "empty_rows"
    RETRYABLE_ERROR = "retryable_error"
    FATAL_ERROR = "fatal_error"


@dataclass(frozen=True, slots=True)
class CloudPriceWorkerConfig:
    job_id: int
    provider: str
    crawl_date: date
    worker_id: str
    batch_size: int = 20
    max_tasks_per_invocation: int = 100
    single_instance_concurrency: int = 1
    request_delay_ms: int = 0
    timeout_seconds: int = 60
    lease_seconds: int = 300
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class WorkerErrorSummary:
    task_id: int
    error_type: str
    retryable: bool
    message: str


@dataclass(slots=True)
class CloudPriceWorkerSummary:
    job_id: int
    worker_id: str
    started_at: datetime
    finished_at: datetime | None = None
    claimed_count: int = 0
    processed_count: int = 0
    succeeded_count: int = 0
    skipped_count: int = 0
    retry_wait_count: int = 0
    dead_letter_count: int = 0
    timeout_count: int = 0
    p50_latency_ms: int = 0
    p95_latency_ms: int = 0
    errors: list[WorkerErrorSummary] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ProviderRowsResult:
    rows: list[dict[str, object]]
    response_hash: str
    http_status: int = 200


class CloudPriceProvider(Protocol):
    async def query_public_price(self, task: CloudStationOdTask) -> ProviderRowsResult: ...


class MockProviderError(Exception):
    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        retryable: bool,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.retryable = retryable
        self.http_status = http_status


class MockCloudPriceProvider:
    def __init__(self, mode: MockProviderMode | str = MockProviderMode.FIXED_ROWS) -> None:
        self.mode = MockProviderMode(mode)
        self.call_count = 0

    async def query_public_price(self, task: CloudStationOdTask) -> ProviderRowsResult:
        self.call_count += 1
        if self.mode == MockProviderMode.EMPTY_ROWS:
            return ProviderRowsResult(rows=[], response_hash=f"empty:{task.id}")
        if self.mode == MockProviderMode.RETRYABLE_ERROR:
            raise MockProviderError("timeout", "request timed out", retryable=True)
        if self.mode == MockProviderMode.FATAL_ERROR:
            raise MockProviderError("station_code_missing", "station code is missing", retryable=False)
        return ProviderRowsResult(
            rows=[{"train_code": "G1", "from": task.from_station, "to": task.to_station}],
            response_hash=f"fixed:{task.id}",
        )


async def run_cloud_price_worker(
    config: CloudPriceWorkerConfig,
    repository: CloudCrawlRepository,
    provider: CloudPriceProvider,
    *,
    now: datetime | None = None,
) -> CloudPriceWorkerSummary:
    started_at = now or datetime.now(timezone.utc)
    summary = CloudPriceWorkerSummary(
        job_id=config.job_id,
        worker_id=config.worker_id,
        started_at=started_at,
    )
    latencies: list[float] = []
    processed_budget = max(config.max_tasks_per_invocation, 0)

    while summary.processed_count < processed_budget:
        remaining = processed_budget - summary.processed_count
        batch_size = min(config.batch_size, remaining)
        if batch_size <= 0:
            break
        claimed = repository.claim_tasks(
            config.job_id,
            batch_size=batch_size,
            worker_id=config.worker_id,
            lease_seconds=config.lease_seconds,
        )
        if not claimed:
            break
        summary.claimed_count += len(claimed)
        await _process_batch(config, repository, provider, claimed, summary, latencies)
        if config.dry_run:
            break

    summary.finished_at = datetime.now(timezone.utc)
    summary.p50_latency_ms = percentile_latency_ms(latencies, 50)
    summary.p95_latency_ms = percentile_latency_ms(latencies, 95)
    return summary


async def _process_batch(
    config: CloudPriceWorkerConfig,
    repository: CloudCrawlRepository,
    provider: CloudPriceProvider,
    claimed: list[ClaimedCloudStationOdTask],
    summary: CloudPriceWorkerSummary,
    latencies: list[float],
) -> None:
    semaphore = asyncio.Semaphore(max(config.single_instance_concurrency, 1))

    async def process_one(index: int, claimed_task: ClaimedCloudStationOdTask) -> None:
        if index > 0 and config.request_delay_ms > 0:
            await asyncio.sleep(config.request_delay_ms / 1000)
        async with semaphore:
            await _process_task(config, repository, provider, claimed_task, summary, latencies)

    await asyncio.gather(*(process_one(index, item) for index, item in enumerate(claimed)))


async def _process_task(
    config: CloudPriceWorkerConfig,
    repository: CloudCrawlRepository,
    provider: CloudPriceProvider,
    claimed_task: ClaimedCloudStationOdTask,
    summary: CloudPriceWorkerSummary,
    latencies: list[float],
) -> None:
    task = claimed_task.task
    started = perf_counter()
    try:
        if config.dry_run:
            repository.release_task(task.id, claimed_task.lease_token)
            return
        result = await asyncio.wait_for(
            provider.query_public_price(task), timeout=config.timeout_seconds
        )
        if not result.rows:
            if repository.mark_skipped(task.id, claimed_task.lease_token, "empty rows"):
                summary.skipped_count += 1
            return
        response_summary = CloudTaskResponseSummary(
            response_hash=result.response_hash,
            row_count=len(result.rows),
            http_status=result.http_status,
        )
        if repository.mark_succeeded(task.id, claimed_task.lease_token, response_summary):
            summary.succeeded_count += 1
    except asyncio.TimeoutError:
        summary.timeout_count += 1
        await _handle_error(
            repository,
            claimed_task,
            summary,
            CloudTaskError("timeout", "provider call timed out", retryable=True),
        )
    except MockProviderError as exc:
        await _handle_error(
            repository,
            claimed_task,
            summary,
            CloudTaskError(
                exc.error_type,
                exc.message,
                retryable=exc.retryable,
                http_status=exc.http_status,
            ),
        )
    finally:
        summary.processed_count += 1
        latencies.append((perf_counter() - started) * 1000)


async def _handle_error(
    repository: CloudCrawlRepository,
    claimed_task: ClaimedCloudStationOdTask,
    summary: CloudPriceWorkerSummary,
    error: CloudTaskError,
) -> None:
    task_id = claimed_task.task.id
    if error.retryable:
        before_dead_letters = len(getattr(repository, "dead_letters", []))
        if repository.mark_retry_wait(task_id, claimed_task.lease_token, error):
            after_dead_letters = len(getattr(repository, "dead_letters", []))
            if after_dead_letters > before_dead_letters:
                summary.dead_letter_count += 1
            else:
                summary.retry_wait_count += 1
    else:
        if repository.mark_dead_letter(task_id, claimed_task.lease_token, error):
            summary.dead_letter_count += 1
    summary.errors.append(
        WorkerErrorSummary(
            task_id=task_id,
            error_type=error.error_type,
            retryable=error.retryable,
            message=error.message,
        )
    )
