import argparse
from datetime import date

import pytest

from app.cloud_crawl.models import CloudStationOdTaskDraft
from app.cloud_crawl.sample_tasks import SampledStationOd
from scripts.prepare_cloud_poc_job import build_preview_drafts, create_poc_job, validate_poc_job_args


class FakePocJobRepository:
    def __init__(self) -> None:
        self.jobs: list[dict[str, object]] = []
        self.tasks: list[CloudStationOdTaskDraft] = []

    def insert_job(
        self,
        *,
        provider: str,
        crawl_date: date,
        job_type: str = "public_price_station_od",
        task_mode: str = "sampled_probe",
        status: str = "pending",
    ) -> int:
        self.jobs.append(
            {
                "provider": provider,
                "crawl_date": crawl_date,
                "job_type": job_type,
                "task_mode": task_mode,
                "status": status,
            }
        )
        return 42

    def insert_task(self, draft: CloudStationOdTaskDraft) -> object:
        self.tasks.append(draft)
        return draft


def make_args(**overrides: object) -> argparse.Namespace:
    values = {
        "provider": "12306-public-price",
        "crawl_date": date(2026, 7, 12),
        "max_tasks": 2,
        "priority_start": 100,
        "max_attempts": 3,
        "job_status": "running",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def samples() -> list[SampledStationOd]:
    return [
        SampledStationOd("北京", "上海"),
        SampledStationOd("北京", "南京南"),
        SampledStationOd("南京南", "上海"),
    ]


def test_validate_poc_job_args_accepts_bounded_real_provider_job() -> None:
    validate_poc_job_args(make_args())


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"provider": "mock-cloud-price"}, "provider must be 12306-public-price"),
        ({"max_tasks": 0}, "1<=--max-tasks<=3"),
        ({"max_tasks": 4}, "1<=--max-tasks<=3"),
        ({"max_attempts": 0}, "1<=--max-attempts<=3"),
        ({"max_attempts": 4}, "1<=--max-attempts<=3"),
    ],
)
def test_validate_poc_job_args_rejects_unsafe_args(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(SystemExit, match=message):
        validate_poc_job_args(make_args(**overrides))


def test_build_preview_drafts_uses_job_id_zero_and_does_not_require_repository() -> None:
    drafts = build_preview_drafts(make_args(max_tasks=1), samples())

    assert len(drafts) == 1
    assert drafts[0].job_id == 0
    assert drafts[0].provider == "12306-public-price"


def test_create_poc_job_creates_job_then_inserts_bounded_tasks() -> None:
    repository = FakePocJobRepository()

    prepared = create_poc_job(repository, args=make_args(max_tasks=2), samples=samples())

    assert prepared.job_id == 42
    assert prepared.task_count == 2
    assert repository.jobs == [
        {
            "provider": "12306-public-price",
            "crawl_date": date(2026, 7, 12),
            "job_type": "public_price_station_od",
            "task_mode": "sampled_probe",
            "status": "running",
        }
    ]
    assert [(task.job_id, task.from_station, task.to_station) for task in repository.tasks] == [
        (42, "北京", "上海"),
        (42, "北京", "南京南"),
    ]
