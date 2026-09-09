import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from app.cloud_crawl.models import (  # noqa: E402
    CloudStationOdTaskDraft,
    CloudStationOdTaskStatus,
    CloudTaskError,
    CloudTaskResponseSummary,
)
from app.cloud_crawl.postgres_repository import PostgresCloudCrawlRepository  # noqa: E402


TEST_DSN = os.getenv("CLOUD_CRAWL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="CLOUD_CRAWL_TEST_DATABASE_URL is required for PostgreSQL repository tests",
)


def make_draft(
    job_id: int,
    from_station: str,
    to_station: str,
    *,
    priority: int = 100,
    next_run_at: datetime | None = None,
    max_attempts: int = 5,
) -> CloudStationOdTaskDraft:
    return CloudStationOdTaskDraft(
        job_id=job_id,
        provider="full-snapshot-adjacent-2026-07-12-cloud",
        crawl_date=date(2026, 7, 12),
        from_station=from_station,
        to_station=to_station,
        priority=priority,
        next_run_at=next_run_at,
        max_attempts=max_attempts,
    )


@pytest.fixture()
def repository() -> PostgresCloudCrawlRepository:
    repo = PostgresCloudCrawlRepository(TEST_DSN or "")
    repo.initialize_schema()
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute("DELETE FROM crawl_cloud_dead_letter")
        conn.execute("DELETE FROM crawl_cloud_station_od_response")
        conn.execute("DELETE FROM crawl_cloud_station_od_task")
        conn.execute("DELETE FROM crawl_cloud_job")
        conn.commit()
    return repo


def insert_job(repository: PostgresCloudCrawlRepository, *, status: str = "pending") -> int:
    return repository.insert_job(
        provider="full-snapshot-adjacent-2026-07-12-cloud",
        crawl_date=date(2026, 7, 12),
        status=status,
    )


def test_claim_sets_lease_fields_and_respects_job_status(repository: PostgresCloudCrawlRepository) -> None:
    runnable_job_id = insert_job(repository, status="running")
    cancelled_job_id = insert_job(repository, status="cancelled")
    runnable_task = repository.insert_task(make_draft(runnable_job_id, "北京", "天津"))
    repository.insert_task(make_draft(cancelled_job_id, "天津", "济南"))

    claimed = repository.claim_tasks(runnable_job_id, batch_size=10, worker_id="worker-a", lease_seconds=300)
    cancelled_claimed = repository.claim_tasks(cancelled_job_id, batch_size=10, worker_id="worker-a", lease_seconds=300)

    stored = repository.get_task(runnable_task.id)
    assert [item.task.id for item in claimed] == [runnable_task.id]
    assert cancelled_claimed == []
    assert stored.status == CloudStationOdTaskStatus.LEASED
    assert stored.leased_by == "worker-a"
    assert stored.lease_token == claimed[0].lease_token
    assert stored.lease_expires_at is not None
    assert stored.attempts == 1


def test_concurrent_claim_does_not_duplicate_tasks(repository: PostgresCloudCrawlRepository) -> None:
    job_id = insert_job(repository, status="running")
    for index in range(8):
        repository.insert_task(make_draft(job_id, f"站{index}", f"站{index + 1}", priority=index))

    def claim(worker_id: str) -> list[int]:
        repo = PostgresCloudCrawlRepository(TEST_DSN or "")
        return [item.task.id for item in repo.claim_tasks(job_id, 5, worker_id, 300)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(claim, ["worker-a", "worker-b"])

    claimed_ids = first + second
    assert len(claimed_ids) == 8
    assert len(set(claimed_ids)) == 8


def test_non_current_lease_token_cannot_mark_succeeded(repository: PostgresCloudCrawlRepository) -> None:
    job_id = insert_job(repository, status="running")
    task = repository.insert_task(make_draft(job_id, "北京", "天津"))
    repository.claim_tasks(job_id, batch_size=1, worker_id="worker-a", lease_seconds=300)

    updated = repository.mark_succeeded(
        task.id,
        "stale-token",
        CloudTaskResponseSummary(response_hash="hash-1", row_count=2),
    )

    assert not updated
    assert repository.get_task(task.id).status == CloudStationOdTaskStatus.LEASED
    assert repository.response_keys() == set()


def test_mark_succeeded_writes_idempotent_response_summary(repository: PostgresCloudCrawlRepository) -> None:
    job_id = insert_job(repository, status="running")
    task = repository.insert_task(make_draft(job_id, "北京", "天津"))
    first_claim = repository.claim_tasks(job_id, batch_size=1, worker_id="worker-a", lease_seconds=300)[0]

    assert repository.mark_succeeded(
        task.id,
        first_claim.lease_token,
        CloudTaskResponseSummary(
            response_hash="hash-1",
            row_count=3,
            matched_train_count=2,
            edge_count=4,
            http_status=200,
        ),
    )

    stored = repository.get_task(task.id)
    assert stored.status == CloudStationOdTaskStatus.SUCCEEDED
    assert stored.response_row_count == 3
    assert stored.matched_train_count == 2
    assert stored.edge_count == 4
    assert repository.response_keys() == {(job_id, task.id, "hash-1")}


def test_retry_wait_and_dead_letter_at_max_attempts(repository: PostgresCloudCrawlRepository) -> None:
    job_id = insert_job(repository, status="running")
    task = repository.insert_task(make_draft(job_id, "北京", "天津", max_attempts=2))

    first = repository.claim_tasks(job_id, batch_size=1, worker_id="worker-a", lease_seconds=300)[0]
    assert repository.mark_retry_wait(
        task.id,
        first.lease_token,
        CloudTaskError("timeout", "request timed out"),
    )
    retry_task = repository.get_task(task.id)
    assert retry_task.status == CloudStationOdTaskStatus.RETRY_WAIT
    assert retry_task.next_run_at is not None

    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "UPDATE crawl_cloud_station_od_task SET next_run_at = %s WHERE id = %s",
            (datetime.now(timezone.utc) - timedelta(seconds=1), task.id),
        )
        conn.commit()

    second = repository.claim_tasks(job_id, batch_size=1, worker_id="worker-a", lease_seconds=300)[0]
    assert repository.mark_retry_wait(
        task.id,
        second.lease_token,
        CloudTaskError("timeout", "request timed out"),
    )

    final_task = repository.get_task(task.id)
    assert final_task.status == CloudStationOdTaskStatus.DEAD_LETTER
    assert repository.dead_letter_count() == 1


def test_reclaim_expired_lease(repository: PostgresCloudCrawlRepository) -> None:
    job_id = insert_job(repository, status="running")
    task = repository.insert_task(make_draft(job_id, "北京", "天津"))
    repository.claim_tasks(job_id, batch_size=1, worker_id="worker-a", lease_seconds=1)

    reclaimed = repository.reclaim_expired_leases(datetime.now(timezone.utc) + timedelta(seconds=2))
    stored = repository.get_task(task.id)

    assert reclaimed == 1
    assert stored.status == CloudStationOdTaskStatus.RETRY_WAIT
    assert stored.lease_token is None
    assert stored.last_error_type == "lease_expired"


def test_release_task_supports_dry_run(repository: PostgresCloudCrawlRepository) -> None:
    job_id = insert_job(repository, status="running")
    task = repository.insert_task(make_draft(job_id, "北京", "天津"))
    claimed = repository.claim_tasks(job_id, batch_size=1, worker_id="worker-a", lease_seconds=300)[0]

    assert repository.release_task(task.id, claimed.lease_token)
    stored = repository.get_task(task.id)
    assert stored.status == CloudStationOdTaskStatus.RETRY_WAIT
    assert stored.last_error_type == "dry_run_released"