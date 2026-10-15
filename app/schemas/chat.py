"""The shared Web and Mobile chat contract."""

from datetime import date, time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


ObjectIdText = Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{24}$")]
SessionToken = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{22,128}$")]
SortOrder = Literal["recommended", "price_low", "price_high", "rating", "distance", "newest"]
BudgetPeriod = Literal["day", "trip"]


class ClientContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    car_type_id: ObjectIdText | None = None
    car_type_ids: list[ObjectIdText] | None = Field(default=None, min_length=1, max_length=20)
    brand_id: ObjectIdText | None = None

    @model_validator(mode="after")
    def coordinates_together(self) -> "ClientContext":
        if (self.lat is None) != (self.lng is None):
            raise ValueError("lat and lng must be supplied together")
        if self.car_type_id and self.car_type_ids:
            raise ValueError("use one car type ID filter at a time")
        return self


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    turn_id: str | None = Field(default=None, min_length=16, max_length=128,
                                pattern=r"^[A-Za-z0-9_-]+$")
    session_id: SessionToken | None = None
    locale: Literal["en"] = "en"
    context: ClientContext = Field(default_factory=ClientContext)

    @field_validator("message")
    @classmethod
    def nonblank_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message cannot be blank")
        return value


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    location: str | None = Field(default=None, max_length=120)
    search: str | None = Field(default=None, max_length=120)
    from_date: date | None = None
    to_date: date | None = None
    from_time: time | None = None
    to_time: time | None = None
    min_price: float | None = Field(default=None, ge=0)
    max_price: float | None = Field(default=None, ge=0)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    car_type_id: ObjectIdText | None = None
    car_type_ids: list[ObjectIdText] | None = Field(default=None, min_length=1, max_length=20)
    brand_id: ObjectIdText | None = None
    car_type_name: str | None = Field(default=None, max_length=60)
    brand_name: str | None = Field(default=None, max_length=60)
    seats: int | None = Field(default=None, ge=1, le=20)
    fuel_type: str | None = Field(default=None, max_length=40)
    fuel_efficiency_preferred: bool | None = None
    transmission: str | None = Field(default=None, max_length=40)
    family_suitable: bool | None = None
    rental_days: int | None = Field(default=None, ge=1, le=365)
    budget_period: BudgetPeriod | None = None
    min_rating: float | None = Field(default=None, ge=0, le=5)
    featured: bool | None = None
    sort_by: SortOrder | None = None

    @model_validator(mode="after")
    def valid_range(self) -> "SearchFilters":
        if self.from_date and self.to_date and self.to_date < self.from_date:
            raise ValueError("to_date must be on or after from_date")
        if (self.from_date and self.to_date and self.from_time and self.to_time
                and self.from_date == self.to_date and self.to_time <= self.from_time):
            raise ValueError("return time must be after pickup time")
        if self.min_price is not None and self.max_price is not None:
            if self.max_price < self.min_price:
                raise ValueError("max_price must be at least min_price")
        if (self.lat is None) != (self.lng is None):
            raise ValueError("lat and lng must be supplied together")
        if self.car_type_id and self.car_type_ids:
            raise ValueError("use one car type ID filter at a time")
        return self

    def query_params(self) -> dict[str, str]:
        local_only = {
            "car_type_name", "brand_name", "seats", "fuel_type",
            "fuel_efficiency_preferred", "transmission", "family_suitable",
            "rental_days", "budget_period",
        }
        values = self.model_dump(exclude_none=True, exclude=local_only)
        def encode(value: object) -> str:
            if isinstance(value, (date, time)):
                return value.isoformat()
            if isinstance(value, bool):
                return str(value).lower()
            if isinstance(value, list):
                return ",".join(value)
            return str(value)
        return {key: encode(value) for key, value in values.items()}


class VehicleCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str | None = None
    location: str | None = None
    price: float | None = None
    price_basis: Literal["day"] | None = None
    rating: float | None = None
    is_featured: bool | None = None
    is_favourite: bool | None = None
    car_type_id: str | None = None
    car_type_name: str | None = None
    brand_id: str | None = None
    brand_name: str | None = None
    seats: int | None = Field(default=None, ge=1, le=20)
    fuel_type: str | None = None
    fuel_efficiency: float | None = Field(default=None, ge=0)
    is_fuel_efficient: bool | None = None
    transmission: str | None = None
    luggage_capacity: int | None = Field(default=None, ge=0)
    is_family_suitable: bool | None = None
    match_reason: str | None = Field(default=None, max_length=240)


class ChatAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["SIGN_IN"]


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: SessionToken | None = None
    status: Literal["clarification", "results", "details", "availability",
                    "unavailable", "guidance", "authentication_required"]
    message: str
    vehicles: list[VehicleCard] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    applied_filters: SearchFilters = Field(default_factory=SearchFilters)
    available: bool | None = None
    data_source: Literal["gear", "demo"] = "gear"
    action: ChatAction | None = None
