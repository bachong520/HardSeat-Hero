import asyncio
from datetime import date, datetime, timezone

from app.cloud_crawl.models import CloudStationOdTask, CloudStationOdTaskDraft, CloudStationOdTaskStatus
from app.cloud_crawl.repository import InMemoryCloudCrawlRepository
from app.cloud_crawl.worker import (
    CloudPriceWorkerConfig,
    MockCloudPriceProvider,
    MockProviderMode,
    ProviderRowsResult,
    run_cloud_price_worker,
)


NOW = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)


class SlowProvider:
    async def query_public_price(self, task: CloudStationOdTask) -> ProviderRowsResult:
        await asyncio.sleep(0.05)
        return ProviderRowsResult(rows=[{"train_code": "G1"}], response_hash=f"slow:{task.id}")


def make_config(**overrides: object) -> CloudPriceWorkerConfig:
    values = {
        "job_id": 1,
        "provider": "mock-cloud-price",
        "crawl_date": date(2026, 7, 12),
        "worker_id": "worker-a",
        "batch_size": 10,
        "max_tasks_per_invocation": 10,
        "single_instance_concurrency": 1,
        "request_delay_ms": 0,
        "timeout_seconds": 60,
        "lease_seconds": 300,
        "dry_run": False,
    }
    values.update(overrides)
    return CloudPriceWorkerConfig(**values)


def make_repository(task_count: int, *, max_attempts: int = 5) -> InMemoryCloudCrawlRepository:
    repository = InMemoryCloudCrawlRepository(now=NOW)
    for index in range(task_count):
        repository.insert_task(
            CloudStationOdTaskDraft(
                job_id=1,
                provider="mock-cloud-price",
                crawl_date=date(2026, 7, 12),
                from_station=f"站{index}",
                to_station=f"站{index + 1}",
                max_attempts=max_attempts,
            )
        )
    return repository


async def test_worker_marks_successful_response_as_succeeded() -> None:
    repository = make_repository(1)

    summary = await run_cloud_price_worker(
        make_config(),
        repository,
        MockCloudPriceProvider(MockProviderMode.FIXED_ROWS),
        now=NOW,
    )

    task = repository.get_task(1)
    assert summary.claimed_count == 1
    assert summary.processed_count == 1
    assert summary.succeeded_count == 1
    assert task.status == CloudStationOdTaskStatus.SUCCEEDED
    assert task.response_row_count == 1


async def test_worker_marks_empty_rows_as_skipped() -> None:
    repository = make_repository(1)

    summary = await run_cloud_price_worker(
        make_config(),
        repository,
        MockCloudPriceProvider(MockProviderMode.EMPTY_ROWS),
        now=NOW,
    )

    task = repository.get_task(1)
    assert summary.skipped_count == 1
    assert task.status == CloudStationOdTaskStatus.SKIPPED
    assert task.last_error == "empty rows"


async def test_worker_marks_timeout_as_retry_wait() -> None:
    repository = make_repository(1)

    summary = await run_cloud_price_worker(
        make_config(timeout_seconds=0.001),
        repository,
        SlowProvider(),
        now=NOW,
    )

    task = repository.get_task(1)
    assert summary.timeout_count == 1
    assert summary.retry_wait_count == 1
    assert task.status == CloudStationOdTaskStatus.RETRY_WAIT
    assert task.next_run_at is not None
    assert task.last_error_type == "timeout"


async def test_worker_marks_station_code_missing_as_dead_letter() -> None:
    repository = make_repository(1)

    summary = await run_cloud_price_worker(
        make_config(),
        repository,
        MockCloudPriceProvider(MockProviderMode.FATAL_ERROR),
        now=NOW,
    )

    task = repository.get_task(1)
    assert summary.dead_letter_count == 1
    assert task.status == CloudStationOdTaskStatus.DEAD_LETTER
    assert repository.dead_letters[0].error_type == "station_code_missing"


async def test_worker_dead_letters_retryable_error_at_max_attempts() -> None:
    repository = make_repository(1, max_attempts=1)

    summary = await run_cloud_price_worker(
        make_config(),
        repository,
        MockCloudPriceProvider(MockProviderMode.RETRYABLE_ERROR),
        now=NOW,
    )

    task = repository.get_task(1)
    assert summary.retry_wait_count == 0
    assert summary.dead_letter_count == 1
    assert task.status == CloudStationOdTaskStatus.DEAD_LETTER


async def test_worker_dry_run_releases_tasks_without_calling_provider() -> None:
    repository = make_repository(2)
    provider = MockCloudPriceProvider(MockProviderMode.FIXED_ROWS)

    summary = await run_cloud_price_worker(
        make_config(dry_run=True, max_tasks_per_invocation=2),
        repository,
        provider,
        now=NOW,
    )

    assert provider.call_count == 0
    assert summary.claimed_count == 2
    assert summary.processed_count == 2
    assert repository.get_task(1).status == CloudStationOdTaskStatus.RETRY_WAIT
    assert repository.get_task(2).status == CloudStationOdTaskStatus.RETRY_WAIT


async def test_worker_max_tasks_per_invocation_limits_processing() -> None:
    repository = make_repository(5)

    summary = await run_cloud_price_worker(
        make_config(batch_size=10, max_tasks_per_invocation=3),
        repository,
        MockCloudPriceProvider(MockProviderMode.FIXED_ROWS),
        now=NOW,
    )

    assert summary.claimed_count == 3
    assert summary.processed_count == 3
    assert summary.succeeded_count == 3
    assert repository.get_task(4).status == CloudStationOdTaskStatus.PENDING
