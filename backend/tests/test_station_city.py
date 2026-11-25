from __future__ import annotations

from pathlib import Path

from app.domain.station_city import (
    city_of_station,
    load_station_city_map,
    normalize_city,
)


def test_normalize_city_uses_province_for_municipalities() -> None:
    # CSV 里直辖市的 city 列是区名，直接用会把北京南映射到丰台区
    assert normalize_city("北京", "丰台区") == "北京"
    assert normalize_city("上海", "闸北区") == "上海"
    assert normalize_city("重庆", "万州区") == "重庆"


def test_normalize_city_uses_city_column_for_regular_provinces() -> None:
    assert normalize_city("广东", "广州") == "广州"
    assert normalize_city("江苏", "南京") == "南京"


def test_normalize_city_falls_back_to_province_and_handles_empty() -> None:
    assert normalize_city("广东", "") == "广东"
    assert normalize_city("", "") == ""
    assert normalize_city(None, None) == ""


def test_load_station_city_map_reads_municipality_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "stations.csv"
    csv_path.write_text(
        "name,telecode,latitude,longitude,centrality_score,province,city,railway_bureau,station_type\n"
        "北京南,VNP,39.86,116.37,100,北京,丰台区,北京铁路局,客运站\n"
        "广州南,IZQ,22.99,113.26,100,广东,广州,广铁集团,客运站\n",
        encoding="utf-8-sig",
    )

    mapping = load_station_city_map(str(csv_path))

    assert mapping == {"北京南": "北京", "广州南": "广州"}


def test_load_station_city_map_returns_empty_when_file_is_missing(tmp_path: Path) -> None:
    assert load_station_city_map(str(tmp_path / "nope.csv")) == {}


def test_city_of_station_maps_the_stations_that_used_to_be_lost() -> None:
    # 这几个正是阶段 6 合并时把节点搞成车站级的站名
    assert city_of_station("北京南") == "北京"
    assert city_of_station("上海虹桥") == "上海"
    assert city_of_station("广州南") == "广州"
    assert city_of_station("惠州南") == "惠州"
    # 名字里不带城市的新站，靠 CSV 的 city 列才对
    assert city_of_station("虎门") == "东莞"


def test_city_of_station_returns_none_for_unknown_or_blank() -> None:
    assert city_of_station("不存在站") is None
    assert city_of_station("") is None
    assert city_of_station(None) is None


def test_city_of_station_accepts_an_explicit_mapping() -> None:
    assert city_of_station("北京南", {"北京南": "首都"}) == "首都"
    assert city_of_station("广州南", {"北京南": "首都"}) is None
