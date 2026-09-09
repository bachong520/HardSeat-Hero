from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from app.cloud_crawl.models import (
    ClaimedCloudStationOdTask,
    CloudStationOdTask,
    CloudStationOdTaskDraft,
    CloudStationOdTaskStatus,
    CloudTaskError,
    CloudTaskResponseSummary,
)
from app.cloud_crawl.retry import calculate_next_run_at


SCHEMA_PATH = Path(__file__).with_name("postgres_schema.sql")


class PostgresCloudCrawlRepository:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def initialize_schema(self) -> None:
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with self._connect() as conn:
            conn.execute(schema_sql)
            conn.commit()

    def insert_job(
        self,
        *,
        provider: str,
        crawl_date: Any,
        job_type: str = "public_price_station_od",
        task_mode: str = "sampled_probe",
        status: str = "pending",
        now: datetime | None = None,
    ) -> int:
        current = now or datetime.now(timezone.utc)
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO crawl_cloud_job (
                    provider, crawl_date, job_type, task_mode, status, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (provider, crawl_date, job_type, task_mode, status, current, current),
            ).fetchone()
            conn.commit()
        return int(row["id"])

    def insert_task(self, draft: CloudStationOdTaskDraft) -> CloudStationOdTask:
        current = datetime.now(timezone.utc)
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO crawl_cloud_station_od_task (
                    job_id, provider, crawl_date, from_station, to_station,
                    from_station_code, to_station_code, priority, max_attempts,
                    next_run_at, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_id, crawl_date, from_station, to_station) DO UPDATE
                SET updated_at = crawl_cloud_station_od_task.updated_at
                RETURNING *
                """,
                (
                    draft.job_id,
                    draft.provider,
                    draft.crawl_date,
                    draft.from_station,
                    draft.to_station,
                    draft.from_station_code,
                    draft.to_station_code,
                    draft.priority,
                    draft.max_attempts,
                    draft.next_run_at,
                    current,
                    current,
                ),
            ).fetchone()
            conn.commit()
        return _task_from_row(row)

    def get_task(self, task_id: int) -> CloudStationOdTask:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM crawl_cloud_station_od_task WHERE id = %s",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return _task_from_row(row)

    def response_keys(self) -> set[tuple[int, int, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT job_id, task_id, response_hash FROM crawl_cloud_station_od_response"
            ).fetchall()
        return {(int(row["job_id"]), int(row["task_id"]), str(row["response_hash"])) for row in rows}

    def dead_letter_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM crawl_cloud_dead_letter").fetchone()
        return int(row["count"])

    def claim_tasks(
        self, job_id: int, batch_size: int, worker_id: str, lease_seconds: int
    ) -> list[ClaimedCloudStationOdTask]:
        if batch_size <= 0:
            return []
        now = datetime.now(timezone.utc)
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        claimed: list[ClaimedCloudStationOdTask] = []
        with self._connect() as conn:
            with conn.transaction():
                picked_rows = conn.execute(
                    """
                    SELECT t.id
                    FROM crawl_cloud_station_od_task t
                    WHERE t.job_id = %s
                      AND t.status IN ('pending', 'retry_wait')
                      AND (t.next_run_at IS NULL OR t.next_run_at <= %s)
                      AND EXISTS (
                          SELECT 1
                          FROM crawl_cloud_job j
                          WHERE j.id = t.job_id
                            AND j.status IN ('pending', 'running')
                      )
                    ORDER BY t.priority ASC, t.id ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    (job_id, now, batch_size),
                ).fetchall()
                for picked in picked_rows:
                    lease_token = str(uuid4())
                    row = conn.execute(
                        """
                        UPDATE crawl_cloud_station_od_task
                        SET status = 'leased',
                            leased_by = %s,
                            lease_token = %s,
                            lease_expires_at = %s,
                            attempts = attempts + 1,
                            updated_at = %s
                        WHERE id = %s
                        RETURNING *
                        """,
                        (worker_id, lease_token, lease_expires_at, now, picked["id"]),
                    ).fetchone()
                    claimed.append(ClaimedCloudStationOdTask(task=_task_from_row(row), lease_token=lease_token))
        return claimed

    def mark_succeeded(
        self, task_id: int, lease_token: str, response_summary: CloudTaskResponseSummary
    ) -> bool:
        now = datetime.now(timezone.utc)
        fetched_at = response_summary.fetched_at or now
        with self._connect() as conn:
            with conn.transaction():
                task_row = self._select_leased_task(conn, task_id, lease_token)
                if task_row is None:
                    return False
                conn.execute(
                    """
                    INSERT INTO crawl_cloud_station_od_response (
                        job_id, task_id, provider, crawl_date, from_station, to_station,
                        status, http_status, row_count, response_hash, object_uri,
                        response_excerpt, fetched_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'succeeded', %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (job_id, task_id, response_hash) DO NOTHING
                    """,
                    (
                        task_row["job_id"],
                        task_row["id"],
                        task_row["provider"],
                        task_row["crawl_date"],
                        task_row["from_station"],
                        task_row["to_station"],
                        response_summary.http_status,
                        response_summary.row_count,
                        response_summary.response_hash,
                        response_summary.object_uri,
                        response_summary.response_excerpt,
                        fetched_at,
                    ),
                )
                cursor = conn.execute(
                    """
                    UPDATE crawl_cloud_station_od_task
                    SET status = 'succeeded',
                        response_row_count = %s,
                        matched_train_count = %s,
                        edge_count = %s,
                        last_http_status = %s,
                        last_response_hash = %s,
                        leased_by = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        next_run_at = NULL,
                        updated_at = %s,
                        finished_at = %s
                    WHERE id = %s
                      AND lease_token = %s
                      AND status = 'leased'
                    """,
                    (
                        response_summary.row_count,
                        response_summary.matched_train_count,
                        response_summary.edge_count,
                        response_summary.http_status,
                        response_summary.response_hash,
                        now,
                        now,
                        task_id,
                        lease_token,
                    ),
                )
                return cursor.rowcount == 1

    def mark_skipped(self, task_id: int, lease_token: str, reason: str) -> bool:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE crawl_cloud_station_od_task
                SET status = 'skipped',
                    response_row_count = 0,
                    matched_train_count = 0,
                    edge_count = 0,
                    last_error_type = 'skipped',
                    last_error = %s,
                    leased_by = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    next_run_at = NULL,
                    updated_at = %s,
                    finished_at = %s
                WHERE id = %s
                  AND lease_token = %s
                  AND status = 'leased'
                """,
                (reason, now, now, task_id, lease_token),
            )
            conn.commit()
        return cursor.rowcount == 1

    def mark_retry_wait(self, task_id: int, lease_token: str, error: CloudTaskError) -> bool:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            with conn.transaction():
                task_row = self._select_leased_task(conn, task_id, lease_token)
                if task_row is None:
                    return False
                if int(task_row["attempts"]) >= int(task_row["max_attempts"]):
                    self._write_dead_letter(conn, task_row, error, now)
                    return True
                next_run_at = calculate_next_run_at(now, int(task_row["attempts"]))
                cursor = conn.execute(
                    """
                    UPDATE crawl_cloud_station_od_task
                    SET status = 'retry_wait',
                        next_run_at = %s,
                        last_error_type = %s,
                        last_error = %s,
                        last_http_status = %s,
                        leased_by = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        updated_at = %s
                    WHERE id = %s
                      AND lease_token = %s
                      AND status = 'leased'
                    """,
                    (next_run_at, error.error_type, error.message, error.http_status, now, task_id, lease_token),
                )
                return cursor.rowcount == 1

    def mark_dead_letter(self, task_id: int, lease_token: str, error: CloudTaskError) -> bool:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            with conn.transaction():
                task_row = self._select_leased_task(conn, task_id, lease_token)
                if task_row is None:
                    return False
                self._write_dead_letter(conn, task_row, error, now)
                return True

    def reclaim_expired_leases(self, now: datetime) -> int:
        reclaimed = 0
        with self._connect() as conn:
            with conn.transaction():
                rows = conn.execute(
                    """
                    SELECT *
                    FROM crawl_cloud_station_od_task
                    WHERE status = 'leased'
                      AND lease_expires_at < %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    (now,),
                ).fetchall()
                for row in rows:
                    if int(row["attempts"]) >= int(row["max_attempts"]):
                        self._write_dead_letter(
                            conn,
                            row,
                            CloudTaskError(
                                "lease_expired",
                                "lease expired before worker committed final status",
                                retryable=True,
                            ),
                            now,
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE crawl_cloud_station_od_task
                            SET status = 'retry_wait',
                                next_run_at = %s,
                                last_error_type = 'lease_expired',
                                last_error = 'lease expired before worker committed final status',
                                leased_by = NULL,
                                lease_token = NULL,
                                lease_expires_at = NULL,
                                updated_at = %s
                            WHERE id = %s
                            """,
                            (calculate_next_run_at(now, int(row["attempts"])), now, row["id"]),
                        )
                    reclaimed += 1
        return reclaimed

    def release_task(self, task_id: int, lease_token: str) -> bool:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE crawl_cloud_station_od_task
                SET status = 'retry_wait',
                    next_run_at = %s,
                    last_error_type = 'dry_run_released',
                    last_error = 'dry-run claimed task released without provider call',
                    leased_by = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    updated_at = %s
                WHERE id = %s
                  AND lease_token = %s
                  AND status = 'leased'
                """,
                (now, now, task_id, lease_token),
            )
            conn.commit()
        return cursor.rowcount == 1

    def _connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def _select_leased_task(
        self, conn: psycopg.Connection[Any], task_id: int, lease_token: str
    ) -> dict[str, Any] | None:
        return conn.execute(
            """
            SELECT *
            FROM crawl_cloud_station_od_task
            WHERE id = %s
              AND lease_token = %s
              AND status = 'leased'
            FOR UPDATE
            """,
            (task_id, lease_token),
        ).fetchone()

    def _write_dead_letter(
        self,
        conn: psycopg.Connection[Any],
        task_row: dict[str, Any],
        error: CloudTaskError,
        now: datetime,
    ) -> None:
        conn.execute(
            """
            UPDATE crawl_cloud_station_od_task
            SET status = 'dead_letter',
                last_error_type = %s,
                last_error = %s,
                last_http_status = %s,
                leased_by = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                next_run_at = NULL,
                updated_at = %s,
                finished_at = %s
            WHERE id = %s
            """,
            (error.error_type, error.message, error.http_status, now, now, task_row["id"]),
        )
        conn.execute(
            """
            INSERT INTO crawl_cloud_dead_letter (
                job_id, task_id, provider, crawl_date, from_station, to_station,
                attempts, error_type, error_message, http_status,
                response_excerpt, worker_id, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                task_row["job_id"],
                task_row["id"],
                task_row["provider"],
                task_row["crawl_date"],
                task_row["from_station"],
                task_row["to_station"],
                task_row["attempts"],
                error.error_type,
                error.message,
                error.http_status,
                error.response_excerpt,
                task_row["leased_by"],
                now,
            ),
        )


def _task_from_row(row: dict[str, Any]) -> CloudStationOdTask:
    return CloudStationOdTask(
        id=int(row["id"]),
        job_id=int(row["job_id"]),
        provider=str(row["provider"]),
        crawl_date=row["crawl_date"],
        from_station=str(row["from_station"]),
        to_station=str(row["to_station"]),
        from_station_code=row["from_station_code"],
        to_station_code=row["to_station_code"],
        priority=int(row["priority"]),
        status=CloudStationOdTaskStatus(row["status"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        next_run_at=row["next_run_at"],
        leased_by=row["leased_by"],
        lease_token=row["lease_token"],
        lease_expires_at=row["lease_expires_at"],
        response_row_count=int(row["response_row_count"]),
        matched_train_count=int(row["matched_train_count"]),
        edge_count=int(row["edge_count"]),
        last_error_type=row["last_error_type"],
        last_error=row["last_error"],
        last_http_status=row["last_http_status"],
        last_response_hash=row["last_response_hash"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
    )