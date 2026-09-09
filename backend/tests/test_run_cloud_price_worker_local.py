import argparse
from datetime import date

import pytest

from app.cloud_crawl.providers import Railway12306CloudPriceProvider
from app.cloud_crawl.repository import InMemoryCloudCrawlRepository
from app.cloud_crawl.worker import MockCloudPriceProvider
from scripts.run_cloud_price_worker_local import build_provider, build_repository, validate_real_provider_gate


def make_args(**overrides: object) -> argparse.Namespace:
    values = {
        "repository": "memory",
        "database_url_env": "CLOUD_CRAWL_DATABASE_URL",
        "job_id": 1,
        "provider": "mock-cloud-price",
        "provider_kind": "mock",
        "mock_mode": "fixed_rows",
        "allow_real_provider": False,
        "crawl_date": date(2026, 7, 12),
        "single_instance_concurrency": 1,
        "request_delay_ms": 0,
        "timeout_seconds": 60,
        "max_tasks_per_invocation": 3,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_build_repository_defaults_to_seeded_in_memory_repository() -> None:
    repository = build_repository(make_args())

    assert isinstance(repository, InMemoryCloudCrawlRepository)
    assert repository.get_task(1).from_station == "站0"
    assert repository.get_task(3).to_station == "站3"


def test_build_postgres_repository_requires_configured_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUD_CRAWL_DATABASE_URL", raising=False)

    with pytest.raises(SystemExit, match="CLOUD_CRAWL_DATABASE_URL is required"):
        build_repository(make_args(repository="postgres"))


def test_build_provider_defaults_to_mock_provider() -> None:
    provider = build_provider(make_args())

    assert isinstance(provider, MockCloudPriceProvider)


def test_real_provider_requires_explicit_allow_flag() -> None:
    with pytest.raises(SystemExit, match="--allow-real-provider is required"):
        build_provider(make_args(provider_kind="railway-12306", allow_real_provider=False))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"single_instance_concurrency": 2}, "single-instance-concurrency=1"),
        ({"max_tasks_per_invocation": 4}, "max-tasks-per-invocation<=3"),
        ({"request_delay_ms": 999}, "request-delay-ms>=1000"),
        ({"timeout_seconds": 9}, "10<=--timeout-seconds<=60"),
        ({"timeout_seconds": 61}, "10<=--timeout-seconds<=60"),
    ],
)
def test_real_provider_rejects_unsafe_initial_poc_profile(overrides: dict[str, object], message: str) -> None:
    safe_profile = {
        "provider_kind": "railway-12306",
        "allow_real_provider": True,
        "request_delay_ms": 1000,
    }
    safe_profile.update(overrides)
    args = make_args(**safe_profile)

    with pytest.raises(SystemExit, match=message):
        validate_real_provider_gate(args)


def test_real_provider_can_be_built_only_with_safe_explicit_profile() -> None:
    provider = build_provider(
        make_args(
            provider_kind="railway-12306",
            allow_real_provider=True,
            request_delay_ms=1000,
            max_tasks_per_invocation=3,
            single_instance_concurrency=1,
            timeout_seconds=10,
        )
    )

    assert isinstance(provider, Railway12306CloudPriceProvider)
