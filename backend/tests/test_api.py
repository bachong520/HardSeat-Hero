import json

from fastapi.testclient import TestClient

from app.adapters.base import TrainDataProvider, TrainDataProviderError
from app.api import routes as api_routes
from app.domain.models import RouteQuery, TrainSegment
from app.main import app


client = TestClient(app)


class FailingProvider(TrainDataProvider):
    name = "failing"

    def candidate_transfer_stations(self, query: RouteQuery) -> list[str]:
        return []

    async def search_segments(self, from_station: str, to_station: str, query: RouteQuery) -> list[TrainSegment]:
        raise TrainDataProviderError("数据源失败")


class FailingRouteSearchEngine:
    source = "failing"
    status = {
        "provider": "failing",
        "search_engine": "candidate",
        "transfer_candidate_enabled": False,
        "max_remote_queries": 0,
        "max_concurrent_remote_queries": 0,
        "last_remote_query_count": 0,
        "last_diagnostics": {
            "remote_query_count": 0,
            "memory_cache_hit_count": 0,
            "expanded_candidates": [],
        },
    }

    async def stream_snapshots(self, query: RouteQuery):
        if False:
            yield {}
        raise TrainDataProviderError("数据源失败")


def test_health() -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_stream_route_emits_snapshot_and_elapsed_metadata() -> None:
    response = client.post(
        "/api/routes/search/stream",
        json={
            "from_station": "北京",
            "to_station": "上海",
            "date": "2026-07-01",
            "max_transfers": 1,
            "min_transfer_minutes": 30,
        },
    )

    assert response.status_code == 200
    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert len(lines) >= 2
    snapshots = [line for line in lines if line.get("type") == "snapshot"]
    assert snapshots
    assert snapshots[0]["query_id"] == "2026-07-01:北京:上海"
    assert snapshots[0]["source"] == "mock"
    assert snapshots[0]["plans"]
    assert isinstance(snapshots[0]["elapsed_ms"], int)
    assert isinstance(snapshots[0]["searched_count"], int)
    assert isinstance(snapshots[0]["pending_count"], int)
    metadata = lines[-1]
    assert metadata["type"] == "metadata"
    assert metadata["query_id"] == "2026-07-01:北京:上海"
    assert metadata["source"] == "mock"
    assert isinstance(metadata["elapsed_ms"], int)
    assert metadata["elapsed_ms"] >= 0


def test_provider_status_exposes_search_engine_state() -> None:
    response = client.get("/api/providers/status")

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "mock"
    assert body["search_engine"] == "candidate"
    assert body["status"] == "ok"
    assert body["online_remote_io"] is False
    assert body["transfer_candidate_enabled"] is False
    assert body["max_remote_queries"] > 0
    assert body["max_concurrent_remote_queries"] > 0
    assert body["last_diagnostics"]["remote_query_count"] >= 0
    assert isinstance(body["last_diagnostics"]["expanded_candidates"], list)


def test_provider_status_exposes_last_search_diagnostics() -> None:
    client.post(
        "/api/routes/search/stream",
        json={
            "from_station": "北京",
            "to_station": "上海",
            "date": "2026-07-01",
            "max_transfers": 1,
            "min_transfer_minutes": 30,
        },
    )

    response = client.get("/api/providers/status")

    assert response.status_code == 200
    diagnostics = response.json()["last_diagnostics"]
    assert diagnostics["remote_query_count"] + diagnostics["memory_cache_hit_count"] >= 1
    assert "南京南" in diagnostics["expanded_candidates"]


def test_stream_route_emits_provider_error(monkeypatch) -> None:
    monkeypatch.setattr(api_routes, "route_search_engine", FailingRouteSearchEngine())

    response = client.post(
        "/api/routes/search/stream",
        json={
            "from_station": "北京",
            "to_station": "上海",
            "date": "2026-07-01",
            "max_transfers": 0,
            "min_transfer_minutes": 30,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"error": "数据源失败"}


def test_route_search_rejects_same_station() -> None:
    response = client.post(
        "/api/routes/search/stream",
        json={
            "from_station": "北京",
            "to_station": "北京",
            "date": "2026-07-01",
            "max_transfers": 1,
            "min_transfer_minutes": 30,
        },
    )

    assert response.status_code == 422


def test_route_search_trims_station_names() -> None:
    response = client.post(
        "/api/routes/search/stream",
        json={
            "from_station": " 北京 ",
            "to_station": " 上海 ",
            "date": "2026-07-01",
            "max_transfers": 1,
            "min_transfer_minutes": 30,
        },
    )

    assert response.status_code == 200
    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    snapshots = [line for line in lines if line.get("type") == "snapshot"]
    assert snapshots
    assert snapshots[0]["plans"]
