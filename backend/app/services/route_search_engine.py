from collections.abc import AsyncIterator
from datetime import datetime
from typing import NotRequired, Protocol, TypedDict

from app.domain.models import RouteQuery, TransferPlan


class StreamSnapshot(TypedDict):
    type: str
    query_id: str
    source: str
    updated_at: datetime
    plans: list[TransferPlan]
    searched_count: int
    pending_count: int
    remote_query_count: int
    final: NotRequired[bool]


class RouteSearchEngine(Protocol):
    @property
    def source(self) -> str:
        ...

    @property
    def status(self) -> dict[str, object]:
        ...

    def stream_snapshots(self, query: RouteQuery) -> AsyncIterator[StreamSnapshot]:
        ...
