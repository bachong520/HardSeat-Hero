import os

from app.adapters.base import TrainDataProvider
from app.services.route_search import ProviderBackedRouteSearchEngine
from app.services.route_search_engine import RouteSearchEngine
from app.services.static_price_repository import SQLiteStaticPriceRepository
from app.services.time_expanded_route_search import TimeExpandedRouteSearchEngine


def create_route_search_engine(provider: TrainDataProvider) -> RouteSearchEngine:
    name = os.getenv("ROUTE_SEARCH_ENGINE", "candidate").strip().lower()
    if name in {"", "candidate", "od-candidate", "legacy"}:
        return ProviderBackedRouteSearchEngine(provider)
    if name in {"time-expanded", "time_expanded"}:
        db_path = os.getenv("TIME_EXPANDED_GRAPH_DB") or os.getenv("STATIC_PRICE_DB")
        if not db_path:
            raise ValueError("TIME_EXPANDED_GRAPH_DB or STATIC_PRICE_DB is required")
        return TimeExpandedRouteSearchEngine(
            repository=SQLiteStaticPriceRepository(db_path),
            provider=os.getenv("TIME_EXPANDED_GRAPH_PROVIDER", "12306-public-price"),
        )
    raise ValueError(f"Unsupported ROUTE_SEARCH_ENGINE: {name}")