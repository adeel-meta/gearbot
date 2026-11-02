"""Offline sample inventory and a small parser for local conversation testing."""

import re

from app.clients.gear import GearUnavailable
from app.clients.llm import Extraction
from app.schemas.chat import SearchFilters, VehicleCard
from app.services.dialogue_policy import place_matches


# Every title is marked as a sample so a screenshot cannot be mistaken for live stock.
SAMPLE_CARS = (
    VehicleCard(id="000000000000000000000001", title="Demo Toyota Camry Sedan",
                location="Dubai Marina", price=240, rating=4.7, is_featured=True),
    VehicleCard(id="000000000000000000000002", title="Demo Kia Sportage SUV",
                location="Dubai Marina", price=420, rating=4.6, is_featured=True),
    VehicleCard(id="000000000000000000000003", title="Demo Nissan X-Trail Family SUV",
                location="Dubai", price=490, rating=4.8, is_featured=False),
    VehicleCard(id="000000000000000000000004", title="Demo Hyundai Accent Economy Sedan",
                location="Abu Dhabi", price=170, rating=4.4, is_featured=False),
    VehicleCard(id="000000000000000000000005", title="Demo Toyota Corolla Sedan",
                location="Sharjah", price=210, rating=4.5, is_featured=False),
)

_TIME_TOKEN = re.compile(
    r"(?<!\d)(?P<hour12>1[0-2]|0?[1-9])(?::(?P<minute12>[0-5]\d))?\s*"
    r"(?P<meridiem>a\.?m\.?|p\.?m\.?)(?![a-z])"
    r"|(?<!\d)(?P<hour24>(?:[01]\d|2[0-3])):(?P<minute24>[0-5]\d)(?!\d)",
    re.IGNORECASE,
)


def _time_values(message: str) -> list[str]:
    values = []
    for match in _TIME_TOKEN.finditer(message):
        if match.group("meridiem"):
            hour = int(match.group("hour12")) % 12
            if match.group("meridiem").casefold().startswith("p"):
                hour += 12
            minute = int(match.group("minute12") or 0)
        else:
            hour = int(match.group("hour24"))
            minute = int(match.group("minute24"))
        values.append(f"{hour:02d}:{minute:02d}")
    return values


class DemoGearClient:
    """Implements only the read methods used by the chat flow."""

    async def search_cars(self, filters: SearchFilters,
                          token: str | None) -> list[VehicleCard]:
        if filters.car_type_id or filters.car_type_ids or filters.brand_id:
            raise GearUnavailable("Sample catalog has no Gear type or brand IDs")
        if filters.sort_by in {"distance", "newest"}:
            raise GearUnavailable("Sample catalog has no distance or creation timestamps")
        cars = list(SAMPLE_CARS)
        if filters.location:
            cars = [car for car in cars
                    if car.location and place_matches(filters.location, car.location)]
        if filters.search:
            term = filters.search.casefold()
            if term in {"family car", "family"}:
                term = "suv"
            cars = [car for car in cars if term in (car.title or "").casefold()]
        if filters.min_price is not None:
            cars = [car for car in cars if car.price is not None and car.price >= filters.min_price]
        if filters.max_price is not None:
            cars = [car for car in cars if car.price is not None and car.price <= filters.max_price]
        if filters.min_rating is not None:
            cars = [car for car in cars if car.rating is not None and car.rating >= filters.min_rating]
        if filters.featured is not None:
            cars = [car for car in cars if car.is_featured is filters.featured]
        if filters.sort_by == "price_low":
            cars.sort(key=lambda car: car.price)
        elif filters.sort_by == "price_high":
            cars.sort(key=lambda car: car.price, reverse=True)
        elif filters.sort_by == "rating":
            cars.sort(key=lambda car: car.rating, reverse=True)
        return cars

    async def car_details(self, car_id: str, token: str | None) -> VehicleCard:
        return self._card(car_id)

    async def check_availability(self, car_id: str, filters: SearchFilters,
                                 token: str | None) -> bool:
        self._card(car_id)
        if not all((filters.from_date, filters.to_date)):
            raise GearUnavailable("Rental dates required")
        return car_id != "000000000000000000000003"

    @staticmethod
    def _card(car_id: str) -> VehicleCard:
        for card in SAMPLE_CARS:
            if card.id == car_id:
                return card
        raise GearUnavailable("Unknown sample car")


class DemoExtractor:
    """Deterministic subset of the chat grammar; no key or network is used."""

    async def extract(self, message: str, current: SearchFilters,
                      result_titles: list[str]) -> Extraction:
        lower = message.casefold()
        reset = bool(re.search(r"\b(reset|start over|new search)\b", lower))
        selected = re.search(r"\boption\s+(\d+)\b", lower)
        selected_index = int(selected.group(1)) if selected else None
        if selected_index is None:
            for word, index in (("first", 1), ("second", 2), ("third", 3)):
                if re.search(rf"\b{word}\b", lower):
                    selected_index = index
                    break
        if selected_index is not None and not 1 <= selected_index <= 50:
            selected_index = None

        dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", message)
        times = _time_values(message)
        # SearchFilters validates calendar dates after extraction.
        valid_dates = dates[:2]

        location = None
        for name in ("Dubai Marina", "Abu Dhabi", "Sharjah", "Ajman", "Dubai"):
            if name.casefold() in lower:
                location = name
                break

        search = None
        for term in ("Toyota", "Kia", "Nissan", "Hyundai", "Sportage", "Camry",
                     "Corolla", "X-Trail", "SUV", "Sedan", "Economy", "Family"):
            if re.search(rf"\b{re.escape(term)}\b", message, re.IGNORECASE):
                search = "SUV" if term == "Family" else term
                break

        max_match = re.search(r"\b(?:under|below|less than|max(?:imum)?)\s*(?:aed\s*)?(\d+(?:\.\d+)?)", lower)
        min_match = re.search(r"\b(?:over|above|more than|min(?:imum)?)\s*(?:aed\s*)?(\d+(?:\.\d+)?)", lower)
        rating_match = re.search(r"\b(?:at least|minimum|over)\s*(\d(?:\.\d)?)\s*(?:stars?|rating)\b|\b(\d(?:\.\d)?)\+?\s*stars?\b", lower)
        sort_by = None
        for pattern, value in (
            (r"\b(?:cheapest|lowest price|price low)\b", "price_low"),
            (r"\b(?:most expensive|highest price|price high)\b", "price_high"),
            (r"\b(?:highest rated|best rated|top rated)\b", "rating"),
            (r"\b(?:closest|nearest|by distance)\b", "distance"),
            (r"\b(?:newest|latest)\b", "newest"),
        ):
            if re.search(pattern, lower):
                sort_by = value
                break
        relative_dates = bool(re.search(r"\b(this weekend|next weekend|weekend|tomorrow|next week)\b", lower))
        near_me = "near me" in lower

        if re.search(r"\b(refund|cancel booking|delete|payment|admin)\b", lower):
            intent = "unsupported"
        elif re.search(r"\b(book|booking|reserve|reservation|vendor confirmation|pending)\b", lower):
            intent = "booking_guidance"
        elif re.search(r"\b(quote|checkout|total price)\b", lower):
            intent = "pricing"
        elif result_titles and re.search(
            r"\b(?:how many|number of).{0,40}\bseat|\bseat(?:ing)?\s+capacity\b|"
            r"\b(?:mileage|fuel[ -]?efficien(?:t|cy))\b",
            lower,
        ):
            intent = "vehicle_specs"
        elif re.search(r"\b(available|availability)\b", lower) and result_titles:
            intent = "availability"
        elif re.search(r"\b(view|detail|tell me about)\b", lower) and result_titles:
            intent = "details"
        else:
            intent = "search"

        return Extraction(
            intent=intent,
            selected_index=selected_index,
            reset_filters=reset,
            needs_dates=relative_dates and not valid_dates,
            needs_location=(near_me or ("family" in lower and relative_dates))
                           and location is None and current.location is None,
            unsupported_constraints=[],
            location=location,
            search=search,
            from_date=valid_dates[0] if valid_dates else None,
            to_date=valid_dates[1] if len(valid_dates) > 1 else None,
            from_time=times[0] if times else None,
            to_time=times[1] if len(times) > 1 else None,
            min_price=float(min_match.group(1)) if min_match else None,
            max_price=float(max_match.group(1)) if max_match else None,
            min_rating=float(next(value for value in rating_match.groups() if value))
                       if rating_match else None,
            featured=(False if re.search(r"\b(?:not|non)[ -]?featured\b", lower)
                      else True if re.search(r"\bfeatured\b", lower) else None),
            sort_by=sort_by,
        )
