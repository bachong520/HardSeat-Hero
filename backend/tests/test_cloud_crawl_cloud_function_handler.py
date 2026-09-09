from datetime import date

import pytest

from app.cloud_crawl.cloud_function_handler import (
    CloudFunctionConfigError,
    cloud_price_worker_handler,
    create_repository,
    parse_worker_config,
)
from app.cloud_crawl.models import CloudStationOdTaskDraft
from app.cloud_crawl.repository import InMemoryCloudCrawlRepository
from app.cloud_crawl.worker import MockCloudPriceProvider, MockProviderMode


class RemainingTimeContext:
    def __init__(self, milliseconds: int) -> None:
        self.milliseconds = milliseconds

    def get_remaining_time_in_millis(self) -> int:
        return self.milliseconds


def test_event_parameters_override_environment_defaults() -> None:
    config = parse_worker_config(
        {
            "job_id": 2,
            "provider": "event-provider",
            "crawl_date": "2026-07-12",
            "batch_size": 3,
            "dry_run": True,
        },
        environ={
            "CLOUD_CRAWL_JOB_ID": "1",
            "CLOUD_CRAWL_PROVIDER": "env-provider",
            "CLOUD_CRAWL_DATE": "2026-07-01",
            "CLOUD_CRAWL_DEFAULT_BATCH_SIZE": "9",
            "CLOUD_CRAWL_DRY_RUN": "false",
        },
    )

    assert config.job_id == 2
    assert config.provider == "event-provider"
    assert config.crawl_date == date(2026, 7, 12)
    assert config.batch_size == 3
    assert config.dry_run is True


def test_missing_required_field_returns_controlled_error() -> None:
    response = cloud_price_worker_handler({}, environ={})

    assert response["ok"] is False
    assert response["error"]["type"] == "config_error"
    assert "job_id" in response["error"]["message"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("batch_size", 0, "batch_size must be positive"),
        ("single_instance_concurrency", 0, "single_instance_concurrency must be positive"),
        ("request_delay_ms", -1, "request_delay_ms must be non-negative"),
        ("timeout_seconds", 0, "timeout_seconds must be positive"),
        ("lease_seconds", 0, "lease_seconds must be positive"),
        ("crawl_date", "not-a-date", "crawl_date must be an ISO date"),
        ("dry_run", "maybe", "dry_run must be a boolean"),
    ],
)
def test_invalid_worker_config_values_are_rejected(field: str, value: object, message: str) -> None:
    event = {"job_id": 1, "provider": "mock-cloud-price", "crawl_date": "2026-07-12"}
    event[field] = value

    with pytest.raises(CloudFunctionConfigError, match=message):
        parse_worker_config(event, environ={})


def test_dry_run_handler_returns_summary_without_provider_call() -> None:
    repository = InMemoryCloudCrawlRepository()
    repository.insert_task(
        CloudStationOdTaskDraft(
            job_id=1,
            provider="mock-cloud-price",
            crawl_date=date(2026, 7, 12),
            from_station="北京",
            to_station="天津",
        )
    )
    provider = MockCloudPriceProvider(MockProviderMode.FIXED_ROWS)

    response = cloud_price_worker_handler(
        {
            "job_id": 1,
            "provider": "mock-cloud-price",
            "crawl_date": "2026-07-12",
            "dry_run": True,
        },
        environ={},
        repository=repository,
        provider=provider,
    )

    assert response["ok"] is True
    assert response["summary"]["claimed_count"] == 1
    assert response["summary"]["processed_count"] == 1
    assert isinstance(response["summary"]["started_at"], str)
    assert isinstance(response["summary"]["finished_at"], str)
    assert response["summary"]["errors"] == []
    assert provider.call_count == 0


def test_remaining_time_too_low_stops_before_claiming_tasks() -> None:
    repository = InMemoryCloudCrawlRepository()
    repository.insert_task(
        CloudStationOdTaskDraft(
            job_id=1,
            provider="mock-cloud-price",
            crawl_date=date(2026, 7, 12),
            from_station="北京",
            to_station="天津",
        )
    )

    response = cloud_price_worker_handler(
        {"job_id": 1, "provider": "mock-cloud-price", "crawl_date": "2026-07-12"},
        RemainingTimeContext(1000),
        environ={},
        repository=repository,
        provider=MockCloudPriceProvider(),
    )

    assert response["ok"] is True
    assert response["summary"]["claimed_count"] == 0
    assert response["summary"]["stopped_reason"] == "remaining_time_too_low"


def test_create_repository_defaults_to_in_memory_repository() -> None:
    assert isinstance(create_repository({}), InMemoryCloudCrawlRepository)
