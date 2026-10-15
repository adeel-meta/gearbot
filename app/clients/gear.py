"""Allowlisted read access to the Gear vehicle search API."""

import asyncio
import re
import math
from urllib.parse import urlparse

import httpx

from app.config import ConfigurationError, Settings
from app.schemas.chat import SearchFilters, VehicleCard


CAR_ID = re.compile(r"^[0-9a-fA-F]{24}$")


class GearUnavailable(Exception):
    """Gear did not return verifiable vehicle data."""


class GearAuthorizationRequired(Exception):
    """Gear requires a customer credential or rejected the supplied one."""


def resolve_catalog_id(payload: object, kind: str, requested_name: str) -> str | None:
    """Resolve a user-visible type/brand name only from Gear's authenticated catalog."""
    if kind not in {"car_type", "brand"} or not isinstance(payload, dict):
        return None
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return None
    keys = (
        ("car_types", "carTypes", "vehicle_types", "vehicleTypes", "types")
        if kind == "car_type" else ("brands", "car_brands", "carBrands")
    )
    items = next((data.get(key) for key in keys if isinstance(data.get(key), list)), None)
    if items is None:
        return None

    def normalize(value: str) -> str:
        value = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
        aliases = {"sport utility vehicle": "suv", "sports utility vehicle": "suv"}
        return aliases.get(value, value.removesuffix("s") if value != "suv" else value)

    wanted = normalize(requested_name)
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _text(item, "name", "title", "label", "car_type_name", "brand_name")
        object_id = _text(item, "_id", "id", "value")
        if name and object_id and CAR_ID.fullmatch(object_id) and normalize(name) == wanted:
            return object_id
    return None


def _text(item: dict, *keys: str) -> str | None:
    value = next((item.get(key) for key in keys if item.get(key) is not None), None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(item: dict, *keys: str) -> float | None:
    value = next((item.get(key) for key in keys if item.get(key) is not None), None)
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        match = re.search(r"\d+(?:\.\d+)?", value.replace(",", ""))
        value = float(match.group()) if match else None
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def _integer(item: dict, *keys: str, maximum: int | None = None) -> int | None:
    value = _number(item, *keys)
    if value is None or not value.is_integer():
        return None
    result = int(value)
    if result < 0 or (maximum is not None and result > maximum):
        return None
    return result


def _boolean(item: dict, *keys: str) -> bool | None:
    value = next((item.get(key) for key in keys if item.get(key) is not None), None)
    return value if isinstance(value, bool) else None


def _spec_blocks(item: dict) -> list[dict]:
    blocks = [item]
    for key in ("specification", "car_specification", "specs", "specifications", "car_spec"):
        block = item.get(key)
        if isinstance(block, dict):
            blocks.append(block)
    return blocks


def _field_from_blocks(blocks: list[dict], reader) -> object | None:
    for block in blocks:
        value = reader(block)
        if value is not None:
            return value
    return None


def vehicle_cards(payload: object) -> list[VehicleCard]:
    """Map only fields actually present in a recognized Gear result list."""
    if not isinstance(payload, dict):
        raise GearUnavailable("Unrecognized Gear response")
    data = payload.get("data")
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = next((data[key] for key in ("cars", "vehicles", "results", "items")
                      if isinstance(data.get(key), list)), None)
    else:
        items = None
    if items is None:
        raise GearUnavailable("Unrecognized Gear response")
    cards: list[VehicleCard] = []
    for item in items:
        if not isinstance(item, dict):
            raise GearUnavailable("Invalid vehicle in Gear response")
        car_id = item.get("_id") or item.get("id")
        if not isinstance(car_id, str) or not CAR_ID.fullmatch(car_id):
            raise GearUnavailable("Missing vehicle ID in Gear response")
        title = item.get("title")
        location = item.get("location")
        price = _number(item, "price", "day_price_with_fee", "day_price")
        rating = item.get("rating") if item.get("rating") is not None else item.get("avg_rating")
        favourite = item.get("is_favourite")
        featured = item.get("is_featured")
        blocks = _spec_blocks(item)
        seats = _field_from_blocks(
            blocks, lambda block: _integer(block, "no_seats", "seats", "seating_capacity", maximum=20),
        )
        fuel_type = _field_from_blocks(
            blocks, lambda block: _text(block, "fuel_type", "fuel", "fuelType"),
        )
        fuel_efficiency = _field_from_blocks(
            blocks,
            lambda block: _number(
                block, "fuel_efficiency", "mileage", "km_per_litre", "km_per_liter",
            ),
        )
        is_fuel_efficient = _field_from_blocks(
            blocks, lambda block: _boolean(block, "is_fuel_efficient", "fuel_efficient"),
        )
        transmission = _field_from_blocks(
            blocks, lambda block: _text(block, "transmission", "transmission_type"),
        )
        luggage_capacity = _field_from_blocks(
            blocks,
            lambda block: _integer(block, "luggage_capacity", "luggage", "bags", maximum=50),
        )
        is_family_suitable = _field_from_blocks(
            blocks, lambda block: _boolean(block, "is_family_suitable", "family_suitable"),
        )
        cards.append(VehicleCard(
            id=car_id,
            title=title if isinstance(title, str) else None,
            location=location if isinstance(location, str) else None,
            price=price,
            price_basis="day" if price is not None else None,
            rating=float(rating) if isinstance(rating, (int, float)) and not isinstance(rating, bool)
                                and math.isfinite(rating) and 0 <= rating <= 5 else None,
            is_featured=featured if isinstance(featured, bool) else None,
            is_favourite=favourite if isinstance(favourite, bool) else None,
            car_type_id=_text(item, "car_type_id"),
            car_type_name=_text(item, "car_type_name", "vehicle_type", "category"),
            brand_id=_text(item, "brand_id"),
            brand_name=_text(item, "brand_name", "make"),
            seats=seats,
            fuel_type=fuel_type,
            fuel_efficiency=fuel_efficiency,
            is_fuel_efficient=is_fuel_efficient,
            transmission=transmission,
            luggage_capacity=luggage_capacity,
            is_family_suitable=is_family_suitable,
        ))
    return cards


class GearClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    async def search_cars(self, filters: SearchFilters, token: str | None) -> list[VehicleCard]:
        params = {"sort_by": "recommended",
                  **filters.query_params()}
        return await self._paged_cards("getCars", None, params)

    async def search_available_cars(self, filters: SearchFilters,
                                    token: str | None) -> list[VehicleCard]:
        self._require_token(token)
        if not (filters.from_date and filters.to_date):
            raise GearUnavailable("Availability search requires exact dates")
        if filters.min_price is not None and filters.max_price is None:
            raise GearUnavailable("Availability endpoint needs a maximum price")
        accepted = {"from_date", "to_date", "from_time", "to_time",
                    "lat", "lng", "min_price", "max_price", "car_type_id", "brand_id"}
        forwarded = filters.query_params()
        if filters.max_price is not None and filters.min_price is None:
            forwarded["min_price"] = "0"
        params = {**{key: value for key, value in forwarded.items()
                                if key in accepted}}
        return await self._paged_cards("getAvailableCars", token, params)

    async def _paged_cards(self, endpoint: str, token: str | None,
                           params: dict[str, str]) -> list[VehicleCard]:
        """Collect complete 50-card pages before applying any local-only filter."""
        collected: list[VehicleCard] = []
        seen_ids: set[str] = set()
        for page in range(1, 11):
            payload = await self._request("GET", endpoint, token,
                                          params={"page": str(page), "limit": "50", **params})
            cards = vehicle_cards(payload)
            for card in cards:
                if card.id not in seen_ids:
                    collected.append(card)
                    seen_ids.add(card.id)
            if len(cards) < 50:
                return collected
        # An unverified partial inventory must not be described as a complete search.
        raise GearUnavailable("Gear result pagination exceeded the verification limit")

    async def car_details(self, car_id: str, token: str | None) -> VehicleCard:
        if not CAR_ID.fullmatch(car_id):
            raise GearUnavailable("Invalid vehicle ID")
        self._require_token(token)
        payload = await self._request("GET", "getCarDetails", token,
                                      params={"car_id": car_id})
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise GearUnavailable("Unrecognized Gear details response")
        item = payload["data"]
        if isinstance(item.get("car"), dict):
            item = item["car"]
        cards = vehicle_cards({"data": [item]})
        if cards[0].id.lower() != car_id.lower():
            raise GearUnavailable("Gear returned a different vehicle")
        return cards[0]

    async def check_availability(self, car_id: str, filters: SearchFilters,
                                 token: str | None) -> bool:
        if not CAR_ID.fullmatch(car_id):
            raise GearUnavailable("Invalid vehicle ID")
        self._require_token(token)
        if not all((filters.from_date, filters.from_time, filters.to_date, filters.to_time)):
            raise GearUnavailable("Rental dates and times are required")
        payload = await self._request("POST", "checkIsCarAvailable", token, body={
            "car_id": car_id,
            "start_date": filters.from_date.isoformat(),
            "start_time": filters.from_time.strftime("%H:%M"),
            "end_date": filters.to_date.isoformat(),
            "end_time": filters.to_time.strftime("%H:%M"),
        })
        if not isinstance(payload, dict):
            raise GearUnavailable("Unrecognized Gear availability response")
        data = payload.get("data")
        for source in (data, payload):
            if isinstance(source, dict):
                for key in ("is_available", "isAvailable", "available"):
                    if isinstance(source.get(key), bool):
                        return source[key]
        if isinstance(data, bool):
            return data
        # Gear's web client treats a 2xx checkIsCarAvailable response as available,
        # unless the API envelope explicitly returns status=false.
        if payload.get("status") is False:
            return False
        return True

    async def filter_catalog(self, token: str | None) -> object:
        self._require_token(token)
        return await self._request("GET", "getDropdownData", token)

    async def default_locations(self, token: str | None) -> object:
        self._require_token(token)
        return await self._request("GET", "getDefaultLocations", token)

    @staticmethod
    def _require_token(token: str | None) -> None:
        if not token:
            raise GearAuthorizationRequired()

    async def _request(self, method: str, endpoint: str, token: str | None,
                       params: dict[str, str] | None = None,
                       body: dict[str, str] | None = None) -> object:
        if endpoint not in {"getCars", "getAvailableCars", "getCarDetails", "checkIsCarAvailable",
                            "getDropdownData", "getDefaultLocations"}:
            raise GearUnavailable("Gear endpoint is not allowlisted")
        base_url = self.settings.gear_api_base_url
        parsed = urlparse(base_url)
        if not base_url or parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ConfigurationError("GEAR_API_BASE_URL must be a valid host URL")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ConfigurationError("GEAR_API_BASE_URL must contain only scheme and host")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise ConfigurationError("Gear API needs HTTPS outside local development")
        url = f"{base_url}{self.settings.gear_api_prefix}/{endpoint}"
        # getCars is public. Protected read routes use the user's token, which Gear validates.
        headers = {"Accept": "application/json", "Language": "en"}
        if endpoint != "getCars" and token:
            headers["Authorization"] = f"Bearer {token}"
        async def request_gear() -> httpx.Response:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout,
                                         transport=self.transport) as client:
                response = await client.request(method, url, params=params, json=body,
                                                headers=headers)
                if endpoint != "getCars" and response.status_code in {401, 403}:
                    raise GearAuthorizationRequired()
                response.raise_for_status()
                return response

        for attempt in range(2):
            try:
                response = await asyncio.wait_for(
                    request_gear(), timeout=self.settings.request_timeout,
                )
                return response.json()
            except GearAuthorizationRequired:
                raise
            except (asyncio.TimeoutError, httpx.TransportError) as exc:
                if attempt == 0:
                    await asyncio.sleep(0.1)
                    continue
                raise GearUnavailable("Gear request failed") from exc
            except (httpx.HTTPStatusError, ValueError) as exc:
                raise GearUnavailable("Gear request failed") from exc
        raise GearUnavailable("Gear request failed")
