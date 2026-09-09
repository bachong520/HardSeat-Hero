from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

from app.cloud_crawl.models import CloudStationOdTaskDraft


@dataclass(frozen=True, slots=True)
class SampledStationOd:
    from_station: str
    to_station: str
    from_station_code: str | None = None
    to_station_code: str | None = None


def read_sampled_station_ods(csv_path: str | Path) -> list[SampledStationOd]:
    path = Path(csv_path)
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None:
            return []
        return [_row_to_sample(row) for row in reader]


def build_sampled_probe_task_drafts(
    samples: Iterable[SampledStationOd],
    *,
    job_id: int,
    provider: str,
    crawl_date: date,
    max_tasks: int,
    priority_start: int = 100,
    max_attempts: int = 5,
) -> list[CloudStationOdTaskDraft]:
    if max_tasks <= 0:
        return []

    drafts: list[CloudStationOdTaskDraft] = []
    seen: set[tuple[str, str]] = set()
    for sample in samples:
        from_station = sample.from_station.strip()
        to_station = sample.to_station.strip()
        if not from_station or not to_station or from_station == to_station:
            continue
        key = (from_station, to_station)
        if key in seen:
            continue
        seen.add(key)
        drafts.append(
            CloudStationOdTaskDraft(
                job_id=job_id,
                provider=provider,
                crawl_date=crawl_date,
                from_station=from_station,
                to_station=to_station,
                from_station_code=_clean_optional(sample.from_station_code),
                to_station_code=_clean_optional(sample.to_station_code),
                priority=priority_start + len(drafts),
                max_attempts=max_attempts,
                metadata={"task_mode": "sampled_probe"},
            )
        )
        if len(drafts) >= max_tasks:
            break
    return drafts


def summarize_sampled_probe_drafts(drafts: Iterable[CloudStationOdTaskDraft]) -> str:
    lines = ["from_station,to_station,from_station_code,to_station_code"]
    for draft in drafts:
        lines.append(
            ",".join(
                [
                    draft.from_station,
                    draft.to_station,
                    draft.from_station_code or "",
                    draft.to_station_code or "",
                ]
            )
        )
    return "\n".join(lines)


def _row_to_sample(row: dict[str, str]) -> SampledStationOd:
    from_station = _first_present(row, "from_station", "origin", "from", "出发站")
    to_station = _first_present(row, "to_station", "destination", "to", "到达站")
    if from_station is None or to_station is None:
        raise ValueError("sample CSV must contain from_station/to_station or origin/destination columns")
    return SampledStationOd(
        from_station=from_station,
        to_station=to_station,
        from_station_code=_first_present(row, "from_station_code", "origin_code", "from_code"),
        to_station_code=_first_present(row, "to_station_code", "destination_code", "to_code"),
    )


def _first_present(row: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is not None and value.strip() != "":
            return value.strip()
    return None


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None