from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path

# 直辖市：CSV 的 city 列填的是区（东城区、闸北区），必须按省覆盖成城市。
MUNICIPALITIES = frozenset({"北京", "上海", "天津", "重庆"})

DEFAULT_STATION_METADATA_CSV = Path(__file__).resolve().parents[1] / "data" / "station_metadata.csv"


def normalize_city(province: str | None, city: str | None) -> str:
    """把 CSV 的 province / city 两列归一成一个"城市名"。

    普通省份直接用 city 列（南京、广州、惠州）；
    直辖市要用 province 覆盖，否则「北京南」会被映射到「丰台区」。
    两列都空时返回空字符串，由调用方决定回落策略。
    """
    province_text = (province or "").strip()
    city_text = (city or "").strip()
    if province_text in MUNICIPALITIES:
        return province_text
    return city_text or province_text


@lru_cache(maxsize=4)
def load_station_city_map(path: str | Path = DEFAULT_STATION_METADATA_CSV) -> dict[str, str]:
    """车站名 → 城市名。文件缺失或读不动时返回空字典（调用方需容忍）。"""
    csv_path = Path(path)
    if not csv_path.exists():
        return {}

    mapping: dict[str, str] = {}
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
            for row in csv.DictReader(file):
                name = (row.get("name") or "").strip()
                city = normalize_city(row.get("province"), row.get("city"))
                if name and city:
                    mapping[name] = city
    except (OSError, ValueError):
        return {}
    return mapping


def city_of_station(name: str | None, mapping: dict[str, str] | None = None) -> str | None:
    """车站名 → 城市名；查不到返回 None（不猜，交给调用方回落）。"""
    station = (name or "").strip()
    if not station:
        return None
    table = load_station_city_map() if mapping is None else mapping
    return table.get(station)
