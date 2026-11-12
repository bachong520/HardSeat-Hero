from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class SeatPrice(BaseModel):
    seat_type: str
    price: Decimal = Field(ge=0)
    remaining: str = "unknown"


class TrainSegment(BaseModel):
    train_no: str
    from_station: str
    to_station: str
    depart_at: datetime
    arrive_at: datetime
    duration_minutes: int = Field(gt=0)
    prices: list[SeatPrice]
    source: str = "mock"
    updated_at: datetime

    @model_validator(mode="after")
    def validate_time_order(self) -> "TrainSegment":
        if self.arrive_at <= self.depart_at:
            raise ValueError("arrive_at must be later than depart_at")
        return self

    @property
    def lowest_price(self) -> Decimal | None:
        if not self.prices:
            return None
        return min(price.price for price in self.prices)


class RouteQuery(BaseModel):
    from_station: str = Field(min_length=1, max_length=64)
    to_station: str = Field(min_length=1, max_length=64)
    date: date
    max_transfers: int = Field(default=1, ge=0, le=2)
    min_transfer_minutes: int = Field(default=30, ge=0, le=360)
    max_transfer_minutes: int = Field(default=720, ge=30, le=2880)
    max_total_duration_minutes: int | None = Field(default=None, ge=60)
    candidate_limit: int = Field(default=50, ge=1, le=200)
    candidate_strategy: Literal["balanced", "direct_corridor", "wide_detour", "hub_first", "exhaustive_budgeted"] = "balanced"
    max_detour_ratio: float | None = Field(default=None, ge=0, le=3)
    max_detour_km: int | None = Field(default=None, ge=0, le=5000)
    corridor_width_km: int | None = Field(default=None, ge=1, le=2000)

    @field_validator("from_station", "to_station", mode="before")
    @classmethod
    def strip_station_name(cls, value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        return value

    @model_validator(mode="after")
    def validate_distinct_stations(self) -> "RouteQuery":
        if self.from_station == self.to_station:
            raise ValueError("from_station and to_station must be different")
        return self


class TransferPlan(BaseModel):
    total_price: Decimal
    total_duration_minutes: int
    transfer_minutes: int
    transfer_stations: list[str]
    segments: list[TrainSegment]


class RouteSearchResponse(BaseModel):
    query_id: str
    source: str
    updated_at: datetime
    elapsed_ms: int = Field(ge=0)
    plans: list[TransferPlan]


class StationMetadata(BaseModel):
    name: str
    telecode: str
    latitude: float
    longitude: float
    centrality_score: float = Field(default=0, ge=0)
