import json
from datetime import datetime, timezone
from time import perf_counter

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.adapters.base import (
    TrainDataBadRequestError,
    TrainDataConfigurationError,
    TrainDataProviderError,
)
from app.adapters.provider_factory import create_train_data_provider
from app.domain.models import RouteQuery
from app.services.route_search_engine_factory import create_route_search_engine


router = APIRouter()
train_data_provider = create_train_data_provider()
route_search_engine = create_route_search_engine(train_data_provider)


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "hardseat-hero-api"}


@router.get("/providers/status")
async def provider_status() -> dict[str, object]:
    status = dict(route_search_engine.status)
    status.setdefault("online_remote_io", False)
    status["updated_at"] = datetime.now(timezone.utc).isoformat()
    return status


@router.post("/routes/search/stream")
async def stream_routes(query: RouteQuery) -> StreamingResponse:
    async def generate():
        started_at = perf_counter()
        try:
            async for snapshot in route_search_engine.stream_snapshots(query):
                payload = {
                    **snapshot,
                    "elapsed_ms": max(0, round((perf_counter() - started_at) * 1000)),
                    "plans": [plan.model_dump(mode="json") for plan in snapshot["plans"]],
                    "updated_at": snapshot["updated_at"].isoformat(),
                }
                yield json.dumps(payload, ensure_ascii=False) + "\n"
            yield json.dumps(
                {
                    "type": "metadata",
                    "query_id": f"{query.date.isoformat()}:{query.from_station}:{query.to_station}",
                    "source": route_search_engine.source,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "elapsed_ms": max(0, round((perf_counter() - started_at) * 1000)),
                },
                ensure_ascii=False,
            ) + "\n"
        except TrainDataProviderError as exc:
            yield json.dumps({"error": str(exc)}, ensure_ascii=False) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


def provider_error_status_code(exc: TrainDataProviderError) -> int:
    if isinstance(exc, TrainDataBadRequestError):
        return 400
    if isinstance(exc, TrainDataConfigurationError):
        return 503
    return 502
