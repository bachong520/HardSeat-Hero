from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4

from app.cloud_crawl.models import (
    ClaimedCloudStationOdTask,
    CloudDeadLetter,
    CloudStationOdResponse,
    CloudStationOdTask,
    CloudStationOdTaskDraft,
    CloudStationOdTaskStatus,
    CloudTaskError,
    CloudTaskResponseSummary,
)
from app.cloud_crawl.retry import calculate_next_run_at


class CloudCrawlRepository(Protocol):
    def claim_tasks(
        self, job_id: int, batch_size: int, worker_id: str, lease_seconds: int
    ) -> list[ClaimedCloudStationOdTask]: ...

    def mark_succeeded(
        self, task_id: int, lease_token: str, response_summary: CloudTaskResponseSummary
    ) -> bool: ...

    def mark_skipped(self, task_id: int, lease_token: str, reason: str) -> bool: ...

    def mark_retry_wait(self, task_id: int, lease_token: str, error: CloudTaskError) -> bool: ...

    def mark_dead_letter(self, task_id: int, lease_token: str, error: CloudTaskError) -> bool: ...

    def reclaim_expired_leases(self, now: datetime) -> int: ...

    def release_task(self, task_id: int, lease_token: str) -> bool: ...


class InMemoryCloudCrawlRepository:
    """Deterministic repository for local worker tests.

    PostgreSQL implementation should claim rows in one transaction with
    `FOR UPDATE SKIP LOCKED`. MySQL 8 can use the same clause where available.
    For stores without skip-locked semantics, use a two-step optimistic update:
    select ordered candidate ids, then update each row with predicates on the
    original status and next_run_at; only rows affected by that conditional
    update are considered claimed.
    """

    def __init__(self, *, now: datetime | None = None) -> None:
        self._tasks: dict[int, CloudStationOdTask] = {}
        self._task_keys: dict[tuple[int, str, str, str], int] = {}
        self._responses: set[tuple[int, int, str]] = set()
        self.dead_letters: list[CloudDeadLetter] = []
        self._next_task_id = 1
        self._next_dead_letter_id = 1
        self._now = now or datetime.now(timezone.utc)

    @property
    def responses(self) -> set[tuple[int, int, str]]:
        return set(self._responses)

    def set_now(self, now: datetime) -> None:
        self._now = now

    def insert_task(self, draft: CloudStationOdTaskDraft) -> CloudStationOdTask:
        key = (draft.job_id, draft.crawl_date.isoformat(), draft.from_station, draft.to_station)
        existing_id = self._task_keys.get(key)
        if existing_id is not None:
            return self.get_task(existing_id)

        task = CloudStationOdTask(
            id=self._next_task_id,
            job_id=draft.job_id,
            provider=draft.provider,
            crawl_date=draft.crawl_date,
            from_station=draft.from_station,
            to_station=draft.to_station,
            from_station_code=draft.from_station_code,
            to_station_code=draft.to_station_code,
            priority=draft.priority,
            max_attempts=draft.max_attempts,
            next_run_at=draft.next_run_at,
            created_at=self._now,
            updated_at=self._now,
        )
        self._next_task_id += 1
        self._task_keys[key] = task.id
        self._tasks[task.id] = task
        return deepcopy(task)

    def get_task(self, task_id: int) -> CloudStationOdTask:
        return deepcopy(self._tasks[task_id])

    def claim_tasks(
        self, job_id: int, batch_size: int, worker_id: str, lease_seconds: int
    ) -> list[ClaimedCloudStationOdTask]:
        candidates = [
            task
            for task in self._tasks.values()
            if task.job_id == job_id
            and task.status in {CloudStationOdTaskStatus.PENDING, CloudStationOdTaskStatus.RETRY_WAIT}
            and (task.next_run_at is None or task.next_run_at <= self._now)
        ]
        candidates.sort(key=lambda task: (task.priority, task.id))

        claimed: list[ClaimedCloudStationOdTask] = []
        for task in candidates[: max(batch_size, 0)]:
            lease_token = str(uuid4())
            task.status = CloudStationOdTaskStatus.LEASED
            task.leased_by = worker_id
            task.lease_token = lease_token
            task.lease_expires_at = self._now + timedelta(seconds=lease_seconds)
            task.attempts += 1
            task.updated_at = self._now
            claimed.append(ClaimedCloudStationOdTask(task=deepcopy(task), lease_token=lease_token))
        return claimed

    def mark_succeeded(
        self, task_id: int, lease_token: str, response_summary: CloudTaskResponseSummary
    ) -> bool:
        task = self._leased_task_or_none(task_id, lease_token)
        if task is None:
            return False
        fetched_at = response_summary.fetched_at or self._now
        response = CloudStationOdResponse(
            job_id=task.job_id,
            task_id=task.id,
            provider=task.provider,
            crawl_date=task.crawl_date,
            from_station=task.from_station,
            to_station=task.to_station,
            status=CloudStationOdTaskStatus.SUCCEEDED.value,
            response_hash=response_summary.response_hash,
            row_count=response_summary.row_count,
            http_status=response_summary.http_status,
            object_uri=response_summary.object_uri,
            response_excerpt=response_summary.response_excerpt,
            fetched_at=fetched_at,
        )
        self._responses.add((response.job_id, response.task_id, response.response_hash))
        task.response_row_count = response_summary.row_count
        task.matched_train_count = response_summary.matched_train_count
        task.edge_count = response_summary.edge_count
        task.last_http_status = response_summary.http_status
        task.last_response_hash = response_summary.response_hash
        self._finish_task(task, CloudStationOdTaskStatus.SUCCEEDED)
        return True

    def mark_skipped(self, task_id: int, lease_token: str, reason: str) -> bool:
        task = self._leased_task_or_none(task_id, lease_token)
        if task is None:
            return False
        task.last_error_type = "skipped"
        task.last_error = reason
        self._finish_task(task, CloudStationOdTaskStatus.SKIPPED)
        return True

    def mark_retry_wait(self, task_id: int, lease_token: str, error: CloudTaskError) -> bool:
        task = self._leased_task_or_none(task_id, lease_token)
        if task is None:
            return False
        if task.attempts >= task.max_attempts:
            return self._write_dead_letter(task, error)

        task.status = CloudStationOdTaskStatus.RETRY_WAIT
        task.next_run_at = calculate_next_run_at(self._now, task.attempts)
        task.last_error_type = error.error_type
        task.last_error = error.message
        task.last_http_status = error.http_status
        self._clear_lease(task)
        task.updated_at = self._now
        return True

    def mark_dead_letter(self, task_id: int, lease_token: str, error: CloudTaskError) -> bool:
        task = self._leased_task_or_none(task_id, lease_token)
        if task is None:
            return False
        return self._write_dead_letter(task, error)

    def reclaim_expired_leases(self, now: datetime) -> int:
        reclaimed = 0
        for task in self._tasks.values():
            if (
                task.status == CloudStationOdTaskStatus.LEASED
                and task.lease_expires_at is not None
                and task.lease_expires_at < now
            ):
                task.status = CloudStationOdTaskStatus.RETRY_WAIT
                task.next_run_at = now
                task.last_error_type = "lease_expired"
                task.last_error = "worker lease expired before completion"
                self._clear_lease(task)
                task.updated_at = now
                reclaimed += 1
        return reclaimed

    def release_task(self, task_id: int, lease_token: str) -> bool:
        task = self._leased_task_or_none(task_id, lease_token)
        if task is None:
            return False
        task.status = CloudStationOdTaskStatus.RETRY_WAIT
        task.next_run_at = self._now
        task.last_error_type = "dry_run_released"
        task.last_error = "dry-run claimed task released without provider call"
        self._clear_lease(task)
        task.updated_at = self._now
        return True

    def _leased_task_or_none(self, task_id: int, lease_token: str) -> CloudStationOdTask | None:
        task = self._tasks.get(task_id)
        if (
            task is None
            or task.status != CloudStationOdTaskStatus.LEASED
            or task.lease_token != lease_token
        ):
            return None
        return task

    def _finish_task(self, task: CloudStationOdTask, status: CloudStationOdTaskStatus) -> None:
        task.status = status
        task.finished_at = self._now
        task.next_run_at = None
        task.updated_at = self._now
        self._clear_lease(task)

    def _clear_lease(self, task: CloudStationOdTask) -> None:
        task.leased_by = None
        task.lease_token = None
        task.lease_expires_at = None

    def _write_dead_letter(self, task: CloudStationOdTask, error: CloudTaskError) -> bool:
        task.last_error_type = error.error_type
        task.last_error = error.message
        task.last_http_status = error.http_status
        self.dead_letters.append(
            CloudDeadLetter(
                id=self._next_dead_letter_id,
                job_id=task.job_id,
                task_id=task.id,
                provider=task.provider,
                crawl_date=task.crawl_date,
                from_station=task.from_station,
                to_station=task.to_station,
                attempts=task.attempts,
                error_type=error.error_type,
                error_message=error.message,
                http_status=error.http_status,
                response_excerpt=error.response_excerpt,
                worker_id=task.leased_by,
                created_at=self._now,
            )
        )
        self._next_dead_letter_id += 1
        self._finish_task(task, CloudStationOdTaskStatus.DEAD_LETTER)
        return True
