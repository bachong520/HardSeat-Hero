import os

from app.adapters.base import TrainDataProvider
from app.services.route_search import OnlineRouteSearchEngine
from app.services.route_search_engine import RouteSearchEngine
from app.services.static_price_repository import SQLiteStaticPriceRepository
from app.services.time_expanded_route_search import TimeExpandedRouteSearchEngine


ONLINE_ENGINE_NAMES = {
    "",
    "online",
    "online-search",
    "online_search",
    "candidate",
    "od-candidate",
    "od_candidate",
    "legacy",
}

OFFLINE_ENGINE_NAMES = {
    "offline",
    "offline-graph",
    "offline_graph",
    "time-expanded",
    "time_expanded",
}

def create_route_search_engine(provider: TrainDataProvider) -> RouteSearchEngine:
    name = os.getenv("ROUTE_SEARCH_ENGINE", "online").strip().lower()
    if name in ONLINE_ENGINE_NAMES:
        return OnlineRouteSearchEngine(provider)
    if name in OFFLINE_ENGINE_NAMES:
        db_path = os.getenv("TIME_EXPANDED_GRAPH_DB") or os.getenv("STATIC_PRICE_DB")
        if not db_path:
            raise ValueError("TIME_EXPANDED_GRAPH_DB or STATIC_PRICE_DB is required")
        return TimeExpandedRouteSearchEngine(
            repository=SQLiteStaticPriceRepository(db_path),
            provider=os.getenv("TIME_EXPANDED_GRAPH_PROVIDER", "12306-public-price"),
        )
    raise ValueError(f"Unsupported ROUTE_SEARCH_ENGINE: {name}")