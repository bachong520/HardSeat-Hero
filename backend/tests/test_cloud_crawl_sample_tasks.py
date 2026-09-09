from datetime import date

import pytest

from app.cloud_crawl.sample_tasks import (
    SampledStationOd,
    build_sampled_probe_task_drafts,
    read_sampled_station_ods,
    summarize_sampled_probe_drafts,
)


def test_build_sampled_probe_task_drafts_deduplicates_and_limits() -> None:
    drafts = build_sampled_probe_task_drafts(
        [
            SampledStationOd("北京", "上海", "BJP", "SHH"),
            SampledStationOd("北京", "上海", "BJP", "SHH"),
            SampledStationOd("北京", "北京"),
            SampledStationOd("南京南", "上海", "NKH", "SHH"),
            SampledStationOd("天津", "济南", "TJP", "JNK"),
        ],
        job_id=7,
        provider="full-snapshot-adjacent-2026-07-12-cloud",
        crawl_date=date(2026, 7, 12),
        max_tasks=2,
    )

    assert [(draft.from_station, draft.to_station) for draft in drafts] == [("北京", "上海"), ("南京南", "上海")]
    assert [draft.priority for draft in drafts] == [100, 101]
    assert drafts[0].from_station_code == "BJP"
    assert drafts[0].to_station_code == "SHH"
    assert drafts[0].metadata == {"task_mode": "sampled_probe"}


def test_read_sampled_station_ods_accepts_seed_od_columns(tmp_path) -> None:
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text("origin,destination\n北京,上海\n南京南,上海\n", encoding="utf-8")

    samples = read_sampled_station_ods(csv_path)

    assert samples == [SampledStationOd("北京", "上海"), SampledStationOd("南京南", "上海")]


def test_read_sampled_station_ods_requires_station_columns(tmp_path) -> None:
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text("a,b\n北京,上海\n", encoding="utf-8")

    with pytest.raises(ValueError, match="sample CSV"):
        read_sampled_station_ods(csv_path)


def test_summarize_sampled_probe_drafts_outputs_reproducible_csv() -> None:
    drafts = build_sampled_probe_task_drafts(
        [SampledStationOd("北京", "上海", "BJP", "SHH")],
        job_id=1,
        provider="mock-cloud-price",
        crawl_date=date(2026, 7, 12),
        max_tasks=10,
    )

    assert summarize_sampled_probe_drafts(drafts) == (
        "from_station,to_station,from_station_code,to_station_code\n" "北京,上海,BJP,SHH"
    )