from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import psycopg

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.run_cloud_price_worker_local import validate_real_provider_gate


DEFAULT_REPORT_TEMPLATE = Path("docs/plans/云函数worker真实接口PoC报告模板.md")


@dataclass(frozen=True, slots=True)
class GateCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class JobTaskSnapshot:
    job_exists: bool
    provider: str | None
    crawl_date: date | None
    task_mode: str | None
    job_status: str | None
    total_tasks: int
    runnable_tasks: int
    leased_tasks: int
    succeeded_tasks: int
    skipped_tasks: int
    dead_letter_tasks: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check the preflight gate before running cloud worker real-provider PoC. Does not access 12306."
    )
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--crawl-date", type=date.fromisoformat, required=True)
    parser.add_argument("--task-mode", default="sampled_probe")
    parser.add_argument("--provider-kind", choices=("mock", "railway-12306"), default="railway-12306")
    parser.add_argument("--allow-real-provider", action="store_true")
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--max-tasks-per-invocation", type=int, default=3)
    parser.add_argument("--single-instance-concurrency", type=int, default=1)
    parser.add_argument("--request-delay-ms", type=int, default=1000)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--database-url-env", default="CLOUD_CRAWL_DATABASE_URL")
    parser.add_argument("--report-template", type=Path, default=DEFAULT_REPORT_TEMPLATE)
    parser.add_argument("--require-pending-tasks", action="store_true", default=True)
    return parser.parse_args()


def evaluate_gate(args: argparse.Namespace, snapshot: JobTaskSnapshot | None = None) -> list[GateCheck]:
    checks: list[GateCheck] = []
    checks.extend(check_profile(args))
    checks.append(check_report_template(args.report_template))

    dsn = os.getenv(args.database_url_env)
    checks.append(GateCheck(args.database_url_env, bool(dsn), "set" if dsn else "missing"))
    if not dsn and snapshot is None:
        return checks

    if snapshot is None:
        snapshot = load_job_task_snapshot(dsn, args.job_id)
    checks.extend(check_job_snapshot(args, snapshot))
    return checks


def check_profile(args: argparse.Namespace) -> list[GateCheck]:
    checks = [
        GateCheck("provider_kind", args.provider_kind == "railway-12306", f"provider_kind={args.provider_kind}"),
    ]
    try:
        validate_real_provider_gate(args)
    except SystemExit as exc:
        checks.append(GateCheck("real_provider_profile", False, str(exc)))
    else:
        checks.append(GateCheck("real_provider_profile", True, "safe initial PoC profile"))
    if args.batch_size > 3:
        checks.append(GateCheck("batch_size", False, "batch_size must be <=3 for initial real-provider PoC"))
    else:
        checks.append(GateCheck("batch_size", True, f"batch_size={args.batch_size}"))
    return checks


def check_report_template(path: Path) -> GateCheck:
    report_path = Path(path)
    return GateCheck("poc_report_template", report_path.exists(), str(report_path))


def load_job_task_snapshot(dsn: str, job_id: int) -> JobTaskSnapshot:
    with psycopg.connect(dsn) as conn:
        job = conn.execute(
            "SELECT provider, crawl_date, task_mode, status FROM crawl_cloud_job WHERE id = %s",
            (job_id,),
        ).fetchone()
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM crawl_cloud_station_od_task WHERE job_id = %s GROUP BY status",
            (job_id,),
        ).fetchall()
    counts = {str(status): int(count) for status, count in rows}
    if job is None:
        return JobTaskSnapshot(False, None, None, None, None, 0, 0, 0, 0, 0, 0)
    total = sum(counts.values())
    return JobTaskSnapshot(
        job_exists=True,
        provider=str(job[0]),
        crawl_date=job[1],
        task_mode=str(job[2]),
        job_status=str(job[3]),
        total_tasks=total,
        runnable_tasks=counts.get("pending", 0) + counts.get("retry_wait", 0),
        leased_tasks=counts.get("leased", 0),
        succeeded_tasks=counts.get("succeeded", 0),
        skipped_tasks=counts.get("skipped", 0),
        dead_letter_tasks=counts.get("dead_letter", 0),
    )


def check_job_snapshot(args: argparse.Namespace, snapshot: JobTaskSnapshot) -> list[GateCheck]:
    if not snapshot.job_exists:
        return [GateCheck("job_exists", False, f"job_id={args.job_id} not found")]

    checks = [
        GateCheck("job_exists", True, f"job_id={args.job_id}"),
        GateCheck("job_provider", snapshot.provider == args.provider, f"expected={args.provider}, actual={snapshot.provider}"),
        GateCheck("job_crawl_date", snapshot.crawl_date == args.crawl_date, f"expected={args.crawl_date}, actual={snapshot.crawl_date}"),
        GateCheck("job_task_mode", snapshot.task_mode == args.task_mode, f"expected={args.task_mode}, actual={snapshot.task_mode}"),
        GateCheck("job_status", snapshot.job_status in {"pending", "running"}, f"status={snapshot.job_status}"),
        GateCheck("sample_size", 1 <= snapshot.total_tasks <= 3, f"total_tasks={snapshot.total_tasks}"),
        GateCheck("runnable_tasks", snapshot.runnable_tasks > 0, f"pending_or_retry_wait={snapshot.runnable_tasks}"),
        GateCheck("no_leased_tasks", snapshot.leased_tasks == 0, f"leased={snapshot.leased_tasks}"),
        GateCheck("no_dead_letters", snapshot.dead_letter_tasks == 0, f"dead_letter={snapshot.dead_letter_tasks}"),
    ]
    return checks


def print_checks(checks: list[GateCheck]) -> None:
    for check in checks:
        status = "PASS" if check.ok else "FAIL"
        print(f"{status} {check.name}: {check.detail}")
    print(f"gate_result={'PASS' if all(check.ok for check in checks) else 'FAIL'}")


def main() -> int:
    checks = evaluate_gate(parse_args())
    print_checks(checks)
    return 0 if all(check.ok for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
