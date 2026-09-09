from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class CloudStationOdTaskStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class CloudCrawlJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class CloudCrawlJob:
    id: int
    provider: str
    crawl_date: date
    job_type: str
    task_mode: str
    status: CloudCrawlJobStatus = CloudCrawlJobStatus.PENDING
    total_count: int = 0
    pending_count: int = 0
    leased_count: int = 0
    succeeded_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    dead_letter_count: int = 0
    config_json: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None
    last_error: str | None = None


@dataclass(slots=True)
class CloudStationOdTask:
    id: int
    job_id: int
    provider: str
    crawl_date: date
    from_station: str
    to_station: str
    from_station_code: str | None = None
    to_station_code: str | None = None
    priority: int = 100
    status: CloudStationOdTaskStatus = CloudStationOdTaskStatus.PENDING
    attempts: int = 0
    max_attempts: int = 5
    next_run_at: datetime | None = None
    leased_by: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    response_row_count: int = 0
    matched_train_count: int = 0
    edge_count: int = 0
    last_error_type: str | None = None
    last_error: str | None = None
    last_http_status: int | None = None
    last_response_hash: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CloudStationOdResponse:
    job_id: int
    task_id: int
    provider: str
    crawl_date: date
    from_station: str
    to_station: str
    status: str
    response_hash: str
    fetched_at: datetime
    http_status: int | None = None
    row_count: int = 0
    object_uri: str | None = None
    response_excerpt: str | None = None


@dataclass(frozen=True, slots=True)
class CloudDeadLetter:
    id: int
    job_id: int
    task_id: int
    provider: str
    crawl_date: date
    from_station: str
    to_station: str
    attempts: int
    error_type: str
    error_message: str
    created_at: datetime
    http_status: int | None = None
    response_excerpt: str | None = None
    worker_id: str | None = None


@dataclass(frozen=True, slots=True)
class CloudTaskError:
    error_type: str
    message: str
    retryable: bool = True
    http_status: int | None = None
    response_excerpt: str | None = None


@dataclass(frozen=True, slots=True)
class CloudTaskResponseSummary:
    response_hash: str
    row_count: int = 0
    matched_train_count: int = 0
    edge_count: int = 0
    http_status: int | None = None
    object_uri: str | None = None
    response_excerpt: str | None = None
    fetched_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ClaimedCloudStationOdTask:
    task: CloudStationOdTask
    lease_token: str


@dataclass(slots=True)
class CloudStationOdTaskDraft:
    job_id: int
    provider: str
    crawl_date: date
    from_station: str
    to_station: str
    from_station_code: str | None = None
    to_station_code: str | None = None
    priority: int = 100
    max_attempts: int = 5
    next_run_at: datetime | None = None
    metadata: dict[str, str] = field(default_factory=dict)
