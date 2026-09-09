from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Protocol


class RandomLike(Protocol):
    def uniform(self, a: float, b: float) -> float: ...


def calculate_retry_delay(
    attempts: int,
    *,
    base_delay_seconds: int = 30,
    max_delay_seconds: int = 3600,
    jitter_ratio: float = 0.2,
    rng: RandomLike | None = None,
) -> timedelta:
    """Return bounded exponential backoff with optional positive jitter."""

    if attempts < 1:
        attempts = 1
    raw_delay = base_delay_seconds * (2 ** (attempts - 1))
    delay_seconds = min(max_delay_seconds, raw_delay)
    jitter_upper_bound = delay_seconds * jitter_ratio
    random_source = rng or random
    jitter_seconds = random_source.uniform(0, jitter_upper_bound) if jitter_upper_bound > 0 else 0
    return timedelta(seconds=delay_seconds + jitter_seconds)


def calculate_next_run_at(now: datetime, attempts: int, **kwargs: object) -> datetime:
    return now + calculate_retry_delay(attempts, **kwargs)
