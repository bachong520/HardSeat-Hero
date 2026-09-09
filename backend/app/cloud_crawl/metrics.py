from __future__ import annotations

from statistics import median, quantiles


def percentile_latency_ms(values: list[float], percentile: int) -> int:
    if not values:
        return 0
    if percentile == 50:
        return int(median(values))
    ordered = sorted(values)
    if len(ordered) == 1:
        return int(ordered[0])
    if percentile == 95:
        return int(quantiles(ordered, n=100, method="inclusive")[94])
    raise ValueError(f"unsupported percentile: {percentile}")
