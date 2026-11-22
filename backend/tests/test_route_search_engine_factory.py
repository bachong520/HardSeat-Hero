import pytest

from app.adapters.mock_provider import MockTrainDataProvider
from app.services.route_search import OnlineRouteSearchEngine
from app.services.route_search_engine_factory import create_route_search_engine
from app.services.time_expanded_route_search import TimeExpandedRouteSearchEngine


def test_create_route_search_engine_defaults_to_online(monkeypatch) -> None:
    monkeypatch.delenv("ROUTE_SEARCH_ENGINE", raising=False)

    engine = create_route_search_engine(MockTrainDataProvider())

    assert isinstance(engine, OnlineRouteSearchEngine)
    assert engine.status["search_engine"] == "online"
    assert engine.status["provider"] == "mock"


@pytest.mark.parametrize(
    "engine_name",
    ["online", "online-search", "online_search", "candidate", "od-candidate", "od_candidate", "legacy"],
)
def test_create_route_search_engine_supports_online_aliases(monkeypatch, engine_name: str) -> None:
    monkeypatch.setenv("ROUTE_SEARCH_ENGINE", engine_name)

    engine = create_route_search_engine(MockTrainDataProvider())

    assert isinstance(engine, OnlineRouteSearchEngine)
    assert engine.status["search_engine"] == "online"


def test_create_route_search_engine_rejects_unknown(monkeypatch) -> None:
    monkeypatch.setenv("ROUTE_SEARCH_ENGINE", "bad")

    with pytest.raises(ValueError, match="Unsupported ROUTE_SEARCH_ENGINE"):
        create_route_search_engine(MockTrainDataProvider())


@pytest.mark.parametrize(
    "engine_name",
    ["offline", "offline-graph", "offline_graph", "time-expanded", "time_expanded"],
)
def test_create_route_search_engine_supports_offline_aliases(monkeypatch, tmp_path, engine_name: str) -> None:
    db_path = tmp_path / "time_expanded.sqlite3"
    monkeypatch.setenv("ROUTE_SEARCH_ENGINE", engine_name)
    monkeypatch.setenv("TIME_EXPANDED_GRAPH_DB", str(db_path))
    monkeypatch.setenv("TIME_EXPANDED_GRAPH_PROVIDER", "provider-x")

    engine = create_route_search_engine(MockTrainDataProvider())

    assert isinstance(engine, TimeExpandedRouteSearchEngine)
    assert engine.status["search_engine"] == "time-expanded"
    assert engine.status["provider"] == "provider-x"


def test_status_payload_keeps_same_keys_across_engines(monkeypatch, tmp_path) -> None:
    # 两个引擎共用同一个 /api/providers/status 响应契约，字段集合一旦分叉，
    # 前端就会在缺失字段上读到 undefined（曾经因此整页白屏）。
    monkeypatch.delenv("ROUTE_SEARCH_ENGINE", raising=False)
    online_engine = create_route_search_engine(MockTrainDataProvider())

    monkeypatch.setenv("ROUTE_SEARCH_ENGINE", "time-expanded")
    monkeypatch.setenv("TIME_EXPANDED_GRAPH_DB", str(tmp_path / "time_expanded.sqlite3"))
    offline_engine = create_route_search_engine(MockTrainDataProvider())

    assert set(offline_engine.status) == set(online_engine.status)
    assert set(offline_engine.status["last_diagnostics"]) == set(
        online_engine.status["last_diagnostics"]
    )


def test_create_route_search_engine_time_expanded_requires_db(monkeypatch) -> None:
    monkeypatch.setenv("ROUTE_SEARCH_ENGINE", "time-expanded")
    monkeypatch.delenv("TIME_EXPANDED_GRAPH_DB", raising=False)
    monkeypatch.delenv("STATIC_PRICE_DB", raising=False)

    with pytest.raises(ValueError, match="TIME_EXPANDED_GRAPH_DB or STATIC_PRICE_DB is required"):
        create_route_search_engine(MockTrainDataProvider())