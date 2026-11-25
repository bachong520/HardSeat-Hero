import json
from datetime import date

import pytest

from app.adapters.railway_12306_public_price import Railway12306Error
from app.cloud_crawl.models import CloudStationOdTask
from app.cloud_crawl.providers import (
    RESPONSE_EXCERPT_MAX_CHARS,
    Railway12306CloudPriceProvider,
    build_response_excerpt,
    build_response_body,
    classify_railway_error,
    hash_public_price_rows,
    is_retryable_railway_error,
)
from app.cloud_crawl.worker import MockProviderError
from app.domain.models import RouteQuery


class FakePublicPriceClient:
    def __init__(self, rows: list[dict[str, object]] | None = None, error: Railway12306Error | None = None) -> None:
        self.rows = rows or []
        self.error = error
        self.calls: list[tuple[str, str, RouteQuery]] = []

    async def query(self, from_station: str, to_station: str, query: RouteQuery) -> list[dict[str, object]]:
        self.calls.append((from_station, to_station, query))
        if self.error is not None:
            raise self.error
        return self.rows


def make_task() -> CloudStationOdTask:
    return CloudStationOdTask(
        id=1,
        job_id=1,
        provider="12306-public-price",
        crawl_date=date(2026, 7, 12),
        from_station="北京",
        to_station="上海",
    )


async def test_railway_provider_queries_client_and_hashes_rows() -> None:
    rows = [{"queryLeftNewDTO": {"station_train_code": "G1", "ze_price": "01000"}}]
    client = FakePublicPriceClient(rows=rows)
    provider = Railway12306CloudPriceProvider(client=client)

    result = await provider.query_public_price(make_task())

    assert result.rows == rows
    assert result.response_hash == hash_public_price_rows(rows)
    assert result.http_status == 200
    assert client.calls[0][0:2] == ("北京", "上海")
    assert client.calls[0][2].date == date(2026, 7, 12)


async def test_railway_provider_maps_station_code_missing_to_non_retryable_error() -> None:
    provider = Railway12306CloudPriceProvider(client=FakePublicPriceClient(error=Railway12306Error("站名无法映射电报码：'北京'")))

    with pytest.raises(MockProviderError) as raised:
        await provider.query_public_price(make_task())

    assert raised.value.error_type == "station_code_missing"
    assert raised.value.retryable is False


@pytest.mark.parametrize(
    ("message", "error_type", "retryable"),
    [
        ("票价接口返回非 JSON", "non_json_response", True),
        ("HTTP 403 Forbidden", "http_403", False),
        ("ReadTimeout: 请求超时", "timeout", True),
        ("其他错误", "provider_error", True),
    ],
)
def test_railway_error_classification(message: str, error_type: str, retryable: bool) -> None:
    assert classify_railway_error(message) == error_type
    assert is_retryable_railway_error(message) is retryable


def test_build_response_excerpt_keeps_every_row_when_small() -> None:
    rows = [{"train_code": "G1"}, {"train_code": "G2"}]

    excerpt = build_response_excerpt(rows)

    assert excerpt is not None
    payload = json.loads(excerpt)
    assert payload["total_rows"] == 2
    assert payload["truncated"] is False
    assert payload["rows"] == rows


def test_build_response_excerpt_truncates_but_keeps_total_rows() -> None:
    rows = [{"train_code": f"G{index}", "seat": "二等座", "price": "07950"} for index in range(500)]

    excerpt = build_response_excerpt(rows)

    assert excerpt is not None
    assert len(excerpt) <= RESPONSE_EXCERPT_MAX_CHARS
    payload = json.loads(excerpt)
    assert payload["total_rows"] == 500
    assert payload["truncated"] is True
    assert 0 < len(payload["rows"]) < 500


def test_build_response_excerpt_returns_none_for_empty_rows() -> None:
    assert build_response_excerpt([]) is None


def test_build_response_body_keeps_every_row_even_when_large() -> None:
    """摘要装不下的数据必须完整留在 response_body 里。"""
    rows = [{"train_code": f"G{index}", "seat": "二等座", "price": "07950"} for index in range(500)]

    body = build_response_body(rows)

    assert body is not None
    payload = json.loads(body)
    assert payload["total_rows"] == 500
    assert payload["truncated"] is False
    assert len(payload["rows"]) == 500
    # 同样的数据，摘要装不下
    assert len(build_response_excerpt(rows) or "") <= RESPONSE_EXCERPT_MAX_CHARS


def test_build_response_body_returns_none_for_empty_rows() -> None:
    assert build_response_body([]) is None


async def test_railway_provider_attaches_response_excerpt() -> None:
    rows = [{"queryLeftNewDTO": {"station_train_code": "G1", "ze_price": "01000"}}]
    provider = Railway12306CloudPriceProvider(client=FakePublicPriceClient(rows=rows))

    result = await provider.query_public_price(make_task())

    assert result.response_excerpt is not None
    payload = json.loads(result.response_excerpt)
    assert payload["total_rows"] == 1
    assert payload["truncated"] is False
