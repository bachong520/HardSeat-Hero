from datetime import datetime
from decimal import Decimal

from app.domain.models import RouteQuery, TrainSegment, TransferPlan


COMFORTABLE_MIN_TRANSFER_MINUTES = 30
COMFORTABLE_MAX_TRANSFER_MINUTES = 120


def transfer_wait_comfort_penalty(plan: TransferPlan) -> int:
    penalty = 0
    for previous, current in zip(plan.segments, plan.segments[1:]):
        wait_minutes = max(0, int((current.depart_at - previous.arrive_at).total_seconds() // 60))
        if wait_minutes < COMFORTABLE_MIN_TRANSFER_MINUTES:
            penalty += (COMFORTABLE_MIN_TRANSFER_MINUTES - wait_minutes) * 10
        elif wait_minutes > COMFORTABLE_MAX_TRANSFER_MINUTES:
            penalty += wait_minutes - COMFORTABLE_MAX_TRANSFER_MINUTES
    return penalty


def plan_sort_key(plan: TransferPlan) -> tuple[Decimal, int, int, int]:
    return (
        plan.total_price,
        transfer_wait_comfort_penalty(plan),
        len(plan.transfer_stations),
        plan.total_duration_minutes,
    )


def plan_within_duration_limit(plan: TransferPlan, query: RouteQuery) -> bool:
    if query.max_total_duration_minutes is None:
        return True
    return plan.total_duration_minutes <= query.max_total_duration_minutes


def plan_identity_key(plan: TransferPlan) -> tuple[tuple[str, str, str, datetime, datetime], ...]:
    return tuple(
        (
            segment.train_no,
            segment.from_station,
            segment.to_station,
            segment.depart_at,
            segment.arrive_at,
        )
        for segment in plan.segments
    )


def deduplicate_plans(plans: list[TransferPlan]) -> list[TransferPlan]:
    unique_plans: list[TransferPlan] = []
    seen: set[tuple[tuple[str, str, str, datetime, datetime], ...]] = set()
    for plan in plans:
        key = plan_identity_key(plan)
        if key in seen:
            continue
        seen.add(key)
        unique_plans.append(plan)
    return unique_plans


def build_plan(segments: list[TrainSegment]) -> TransferPlan:
    total_price = sum((segment.lowest_price or Decimal("0") for segment in segments), Decimal("0"))
    total_duration = max(0, int((segments[-1].arrive_at - segments[0].depart_at).total_seconds() // 60))
    ride_minutes = sum(segment.duration_minutes for segment in segments)
    return TransferPlan(
        total_price=total_price,
        total_duration_minutes=total_duration,
        transfer_minutes=max(0, total_duration - ride_minutes),
        transfer_stations=[segment.to_station for segment in segments[:-1]],
        segments=segments,
    )