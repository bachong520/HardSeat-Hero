from collections.abc import AsyncIterator
from typing import Protocol

from app.domain.models import RouteQuery
from app.services.route_search import StreamSnapshot


class RouteSearchEngine(Protocol):
    @property
    def source(self) -> str:
        ...

    @property
    def status(self) -> dict[str, object]:
        ...

    def stream_snapshots(self, query: RouteQuery) -> AsyncIterator[StreamSnapshot]:
        ...
