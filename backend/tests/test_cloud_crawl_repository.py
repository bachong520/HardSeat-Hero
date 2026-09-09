from datetime import date, datetime, timedelta, timezone

from app.cloud_crawl.models import (
    CloudStationOdTaskDraft,
    CloudStationOdTaskStatus,
    CloudTaskError,
    CloudTaskResponseSummary,
)
from app.cloud_crawl.repository import InMemoryCloudCrawlRepository
from app.cloud_crawl.retry import calculate_retry_delay


NOW = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)


def make_draft(
    from_station: str,
    to_station: str,
    *,
    job_id: int = 1,
    next_run_at: datetime | None = None,
    priority: int = 100,
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


def claim_one(repository: InMemoryCloudCrawlRepository):
    claimed = repository.claim_tasks(1, batch_size=1, worker_id="worker-a", lease_seconds=300)
    assert len(claimed) == 1
    return claimed[0]


def test_claim_only_picks_pending_or_due_retry_wait_tasks() -> None:
    repository = InMemoryCloudCrawlRepository(now=NOW)
    pending = repository.insert_task(make_draft("北京", "天津", priority=2))
    future_retry = repository.insert_task(
        make_draft("天津", "济南", next_run_at=NOW + timedelta(minutes=5), priority=1)
    )
    due_retry = repository.insert_task(
        make_draft("济南", "南京", next_run_at=NOW - timedelta(seconds=1), priority=0)
    )

    first_claimed = claim_one(repository)
    second_claimed = claim_one(repository)

    assert [first_claimed.task.id, second_claimed.task.id] == [due_retry.id, pending.id]
    assert repository.get_task(future_retry.id).status == CloudStationOdTaskStatus.PENDING


def test_claim_sets_lease_fields_and_increments_attempts() -> None:
    repository = InMemoryCloudCrawlRepository(now=NOW)
    task = repository.insert_task(make_draft("北京", "天津"))

    claimed = claim_one(repository)
    stored = repository.get_task(task.id)

    assert claimed.task.status == CloudStationOdTaskStatus.LEASED
    assert stored.status == CloudStationOdTaskStatus.LEASED
    assert stored.leased_by == "worker-a"
    assert stored.lease_token == claimed.lease_token
    assert stored.lease_expires_at == NOW + timedelta(seconds=300)
    assert stored.attempts == 1


def test_non_current_lease_token_cannot_mark_succeeded() -> None:
    repository = InMemoryCloudCrawlRepository(now=NOW)
    task = repository.insert_task(make_draft("北京", "天津"))
    claim_one(repository)

    updated = repository.mark_succeeded(
        task.id,
        lease_token="stale-token",
        response_summary=CloudTaskResponseSummary(response_hash="hash-1", row_count=2),
    )

    assert not updated
    assert repository.get_task(task.id).status == CloudStationOdTaskStatus.LEASED
    assert repository.responses == set()


def test_mark_succeeded_writes_idempotent_response_summary() -> None:
    repository = InMemoryCloudCrawlRepository(now=NOW)
    task = repository.insert_task(make_draft("北京", "天津"))
    claimed = claim_one(repository)

    updated = repository.mark_succeeded(
        task.id,
        claimed.lease_token,
        CloudTaskResponseSummary(
            response_hash="hash-1",
            row_count=3,
            matched_train_count=2,
            edge_count=4,
            http_status=200,
        ),
    )

    stored = repository.get_task(task.id)
    assert updated
    assert stored.status == CloudStationOdTaskStatus.SUCCEEDED
    assert stored.lease_token is None
    assert stored.response_row_count == 3
    assert stored.matched_train_count == 2
    assert stored.edge_count == 4
    assert repository.responses == {(1, task.id, "hash-1")}


def test_expired_lease_can_be_reclaimed_to_retry_wait() -> None:
    repository = InMemoryCloudCrawlRepository(now=NOW)
    task = repository.insert_task(make_draft("北京", "天津"))
    claim_one(repository)

    reclaimed_at = NOW + timedelta(seconds=301)
    reclaimed_count = repository.reclaim_expired_leases(reclaimed_at)
    stored = repository.get_task(task.id)

    assert reclaimed_count == 1
    assert stored.status == CloudStationOdTaskStatus.RETRY_WAIT
    assert stored.next_run_at == reclaimed_at
    assert stored.lease_token is None
    assert stored.last_error_type == "lease_expired"


def test_duplicate_task_key_is_idempotent() -> None:
    repository = InMemoryCloudCrawlRepository(now=NOW)
    first = repository.insert_task(make_draft("北京", "天津"))
    second = repository.insert_task(make_draft("北京", "天津", priority=1))

    assert second.id == first.id
    assert repository.claim_tasks(1, batch_size=10, worker_id="worker-a", lease_seconds=300)[0].task.id == first.id


def test_retry_wait_sets_next_run_at_and_dead_letters_at_max_attempts() -> None:
    repository = InMemoryCloudCrawlRepository(now=NOW)
    task = repository.insert_task(make_draft("北京", "天津", max_attempts=2))

    first = claim_one(repository)
    assert repository.mark_retry_wait(
        task.id,
        first.lease_token,
        CloudTaskError(error_type="timeout", message="request timed out"),
    )
    retry_task = repository.get_task(task.id)
    assert retry_task.status == CloudStationOdTaskStatus.RETRY_WAIT
    assert retry_task.next_run_at is not None
    assert retry_task.next_run_at > NOW

    repository.set_now(retry_task.next_run_at)
    second = claim_one(repository)
    assert repository.mark_retry_wait(
        task.id,
        second.lease_token,
        CloudTaskError(error_type="timeout", message="request timed out"),
    )

    final_task = repository.get_task(task.id)
    assert final_task.status == CloudStationOdTaskStatus.DEAD_LETTER
    assert len(repository.dead_letters) == 1
    assert repository.dead_letters[0].error_type == "timeout"


def test_calculate_retry_delay_uses_bounded_exponential_backoff_without_jitter() -> None:
    assert calculate_retry_delay(1, jitter_ratio=0) == timedelta(seconds=30)
    assert calculate_retry_delay(3, jitter_ratio=0) == timedelta(seconds=120)
    assert calculate_retry_delay(10, jitter_ratio=0) == timedelta(seconds=3600)
