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
            response_excerpt=build_response_excerpt(rows),
            response_body=build_response_body(rows),
        )


RESPONSE_EXCERPT_MAX_CHARS = 8000

_EXCERPT_ENVELOPE_CHARS = len('{"total_rows":000000,"truncated":false,"rows":[]}') + 1


def build_response_excerpt(
    rows: list[dict[str, Any]],
    *,
    max_chars: int = RESPONSE_EXCERPT_MAX_CHARS,
) -> str | None:
    """把一次查询的原始行压成可入库的摘要文本。

    返回统一的 JSON 信封：``{"total_rows": 总数, "truncated": 是否截断, "rows": 保留的行}``。
    用信封而不是裸数组，是为了让读的人一眼看出「这是全部还是前几条」——
    没有 total_rows 的话，335 行被截成 40 行，和本来就只返回 40 行，长得一模一样。
    """
    return _build_envelope(rows, max_chars=max_chars)


def build_response_body(rows: list[dict[str, Any]]) -> str | None:
    """完整响应体：与摘要同一个信封，但不截断（``truncated`` 恒为 false）。

    摘要有 8 KB 上限，335 趟车的线路只装得下十来趟；合并要用全量就只能存这里。
    PostgreSQL 的 TEXT 足够装下（实测一条 335 行的响应约 300 KB）。
    """
    return _build_envelope(rows, max_chars=None)


def _build_envelope(rows: list[dict[str, Any]], *, max_chars: int | None) -> str | None:
    if not rows:
        return None

    if max_chars is None:
        kept = list(rows)
    else:
        budget = max(max_chars - _EXCERPT_ENVELOPE_CHARS, 0)
        kept = []
        used = 0
        for row in rows:
            item_chars = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            extra = item_chars + (1 if kept else 0)  # 行与行之间还有一个逗号
            if used + extra > budget and kept:
                break
            kept.append(row)
            used += extra

    payload = {
        "total_rows": len(rows),
        "truncated": len(kept) < len(rows),
        "rows": kept,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


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
