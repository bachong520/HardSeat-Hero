import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.domain.models import TrainSegment


@dataclass
class SearchDiagnostics:
    remote_query_count: int = 0
    memory_cache_hit_count: int = 0
    persistent_cache_hit_count: int = 0
    expanded_od_targets: list[str] = field(default_factory=list)
    failed_od_targets: list[str] = field(default_factory=list)
    pruned_by_complete_price_bound_count: int = 0
    pruned_by_pareto_count: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "remote_query_count": self.remote_query_count,
            "memory_cache_hit_count": self.memory_cache_hit_count,
            "persistent_cache_hit_count": self.persistent_cache_hit_count,
            "expanded_od_targets": self.expanded_od_targets,
            "failed_od_targets": self.failed_od_targets,
            "pruned_by_complete_price_bound_count": self.pruned_by_complete_price_bound_count,
            "pruned_by_pareto_count": self.pruned_by_pareto_count,
        }


class OnlineSearchContext:
    def __init__(self) -> None:
        self.remote_query_count = 0
        self.diagnostics = SearchDiagnostics()
        self.remote_query_lock = asyncio.Lock()


@dataclass(frozen=True)
class OnlineSearchState:
    current_station: str
    segments: tuple[TrainSegment, ...]
    accumulated_price: Decimal
    arrive_at: datetime | None
    visited_stations: frozenset[str]

    @property
    def transfer_count(self) -> int:
        return max(0, len(self.segments) - 1)


@dataclass(frozen=True)
class FrontierEntry:
    price: Decimal
    arrive_at: datetime


class OnlineSearchParetoFrontier:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, int], list[FrontierEntry]] = {}

    def add_if_not_dominated(self, state: OnlineSearchState) -> bool:
        if state.arrive_at is None:
            return True

        key = (state.current_station, state.transfer_count)
        entries = self._entries.setdefault(key, [])
        for entry in entries:
            if entry.price <= state.accumulated_price and entry.arrive_at <= state.arrive_at:
                return False

        entries[:] = [
            entry
            for entry in entries
            if not (state.accumulated_price <= entry.price and state.arrive_at <= entry.arrive_at)
        ]
        entries.append(FrontierEntry(price=state.accumulated_price, arrive_at=state.arrive_at))
        return True