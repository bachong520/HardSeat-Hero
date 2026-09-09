import argparse
from datetime import date

from scripts.check_cloud_poc_gate import JobTaskSnapshot, check_job_snapshot, check_profile, evaluate_gate


def make_args(**overrides: object) -> argparse.Namespace:
    values = {
        "job_id": 1,
        "provider": "12306-public-price",
        "crawl_date": date(2026, 7, 12),
        "task_mode": "sampled_probe",
        "provider_kind": "railway-12306",
        "allow_real_provider": True,
        "batch_size": 3,
        "max_tasks_per_invocation": 3,
        "single_instance_concurrency": 1,
        "request_delay_ms": 1000,
        "timeout_seconds": 60,
        "lease_seconds": 300,
        "database_url_env": "CLOUD_CRAWL_DATABASE_URL",
        "report_template": __file__,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def make_snapshot(**overrides: object) -> JobTaskSnapshot:
    values = {
        "job_exists": True,
        "provider": "12306-public-price",
        "crawl_date": date(2026, 7, 12),
        "task_mode": "sampled_probe",
        "job_status": "running",
        "total_tasks": 3,
        "runnable_tasks": 3,
        "leased_tasks": 0,
        "succeeded_tasks": 0,
        "skipped_tasks": 0,
        "dead_letter_tasks": 0,
    }
    values.update(overrides)
    return JobTaskSnapshot(**values)


def test_check_profile_accepts_safe_initial_real_provider_profile() -> None:
    checks = check_profile(make_args())

    assert all(check.ok for check in checks)


def test_check_profile_rejects_missing_real_provider_allow_flag() -> None:
    checks = check_profile(make_args(allow_real_provider=False))

    assert any(check.name == "real_provider_profile" and not check.ok for check in checks)


def test_check_job_snapshot_accepts_small_runnable_sample() -> None:
    checks = check_job_snapshot(make_args(), make_snapshot())

    assert all(check.ok for check in checks)


def test_check_job_snapshot_rejects_completed_job_without_runnable_tasks() -> None:
    checks = check_job_snapshot(make_args(), make_snapshot(total_tasks=3, runnable_tasks=0, succeeded_tasks=3))

    assert any(check.name == "runnable_tasks" and not check.ok for check in checks)


def test_check_job_snapshot_rejects_large_sample() -> None:
    checks = check_job_snapshot(make_args(), make_snapshot(total_tasks=4, runnable_tasks=4))

    assert any(check.name == "sample_size" and not check.ok for check in checks)


def test_evaluate_gate_uses_injected_snapshot_without_database(monkeypatch) -> None:
    monkeypatch.delenv("CLOUD_CRAWL_DATABASE_URL", raising=False)

    checks = evaluate_gate(make_args(), snapshot=make_snapshot())

    assert any(check.name == "CLOUD_CRAWL_DATABASE_URL" and not check.ok for check in checks)
    assert any(check.name == "job_exists" and check.ok for check in checks)
