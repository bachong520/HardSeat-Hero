"""阶段 6 离线合并的单元测试：只测纯函数，不连数据库。"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest

from scripts.merge_cloud_responses_to_edges import (
    dto_to_edges,
    merge,
    parse_excerpt,
)


FETCHED_AT = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def make_dto(**overrides: object) -> dict[str, object]:
    dto: dict[str, object] = {
        "train_no": "240000G5200G",
        "station_train_code": "G520",
        "from_station_name": "北京南",
        "to_station_name": "上海虹桥",
        "start_time": "07:40",
        "arrive_time": "12:32",
        "day_difference": "0",
        "lishi": "04:52",
        "swz_price": "27820",
        "zy_price": "12720",
        "ze_price": "07950",
    }
    dto.update(overrides)
    return dto


def test_parse_excerpt_keeps_total_rows_and_truncated_flag() -> None:
    text = json.dumps({"total_rows": 335, "truncated": True, "rows": [{"a": 1}, {"a": 2}]})

    rows, total_rows, truncated = parse_excerpt(text)

    assert len(rows) == 2
    assert total_rows == 335
    assert truncated is True


def test_parse_excerpt_rejects_a_bare_list() -> None:
    with pytest.raises(ValueError):
        parse_excerpt(json.dumps([{"a": 1}]))


def test_dto_to_edges_uses_task_city_names_instead_of_station_names() -> None:
    """响应里的车站名是「北京南→上海虹桥」，图里的节点必须是任务的城市名。

    否则用户搜「北京→上海」只能命中恰好叫「北京」的那几趟车。
    """
    edges = dto_to_edges(
        make_dto(),
        origin="北京",
        destination="上海",
        target_provider="12306-public-price",
        fetched_at=FETCHED_AT,
    )

    assert len(edges) == 3  # 商务座 / 一等座 / 二等座
    assert {edge.train_code for edge in edges} == {"G520"}
    assert {edge.from_station for edge in edges} == {"北京"}
    assert {edge.to_station for edge in edges} == {"上海"}
    assert {edge.seat_type for edge in edges} == {"商务座", "一等座", "二等座"}
    assert {edge.depart_time for edge in edges} == {time(7, 40)}
    # 价格单位是「角」，07950 要变成 795 元
    second_class = next(edge for edge in edges if edge.seat_type == "二等座")
    assert second_class.price == Decimal("795")
    # 车站名没有丢，被写进了 raw_hash，保留可追溯性
    assert len({edge.raw_hash for edge in edges}) == 3


def test_dto_to_edges_marks_overnight_arrival() -> None:
    edges = dto_to_edges(
        make_dto(arrive_time="04:01", day_difference="1", ze_price=None, zy_price=None),
        origin="北京",
        destination="上海",
        target_provider="12306-public-price",
        fetched_at=FETCHED_AT,
    )

    assert len(edges) == 1  # 只留商务座
    assert edges[0].arrive_day_offset == 1


def test_dto_to_edges_skips_rows_without_usable_time() -> None:
    assert (
        dto_to_edges(
            make_dto(start_time="", lishi=""),
            origin="北京",
            destination="上海",
            target_provider="12306-public-price",
            fetched_at=FETCHED_AT,
        )
        == []
    )


def test_merge_reports_truncated_tasks() -> None:
    responses = [
        {
            "task_id": 4,
            "from_station": "北京",
            "to_station": "上海",
            "crawl_date": date(2026, 11, 27),
            "row_count": 52,
            "response_excerpt": json.dumps(
                {"total_rows": 52, "truncated": True, "rows": [{"queryLeftNewDTO": make_dto()}]}
            ),
        }
    ]

    edges, report = merge(responses, "12306-public-price")

    assert len(edges) == 3
    assert report[0]["rows_stored"] == 1
    assert report[0]["rows_total"] == 52
    assert report[0]["truncated"] is True
