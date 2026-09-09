from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from app.adapters.railway_12306_public_price import PublicPriceClient, Railway12306Error, StationCodeRepository
from app.cloud_crawl.models import CloudStationOdTask
from app.cloud_crawl.worker import MockProviderError, ProviderRowsResult
from app.domain.models import RouteQuery


class PublicPriceQueryClient(Protocol):
    async def query(self, from_station: str, to_station: str, query: RouteQuery) -> list[dict[str, Any]]: ...


class Railway12306CloudPriceProvider:
    """Cloud worker adapter for the 12306 public price endpoint.

    Construction is side-effect free. Network access only happens when
    ``query_public_price`` is called by the worker.
    """

    def __init__(self, client: PublicPriceQueryClient | None = None, timeout_seconds: float = 10.0) -> None:
        self.client = client or PublicPriceClient(StationCodeRepository(timeout_seconds=timeout_seconds), timeout_seconds)

    async def query_public_price(self, task: CloudStationOdTask) -> ProviderRowsResult:
        query = RouteQuery(from_station=task.from_station, to_station=task.to_station, date=task.crawl_date)
        try:
            rows = await self.client.query(task.from_station, task.to_station, query)
        except Railway12306Error as exc:
            message = str(exc)
            raise MockProviderError(
                classify_railway_error(message),
                message,
                retryable=is_retryable_railway_error(message),
            ) from exc
        return ProviderRowsResult(
            rows=rows,
            response_hash=hash_public_price_rows(rows),
            http_status=200,
        )


def hash_public_price_rows(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify_railway_error(message: str) -> str:
    if "站名无法映射电报码" in message:
        return "station_code_missing"
    if "非 JSON" in message:
        return "non_json_response"
    if "HTTP 403" in message:
        return "http_403"
    if "请求超时" in message or "Timeout" in message:
        return "timeout"
    return "provider_error"


def is_retryable_railway_error(message: str) -> bool:
    if "站名无法映射电报码" in message:
        return False
    if "HTTP 403" in message:
        return False
    return True
