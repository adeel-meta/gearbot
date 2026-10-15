"""Block requests for Gear filters whose API mapping is not confirmed."""

import re


UNVERIFIED_FILTERS = {
    "seats": re.compile(r"\b(?:\d+\s*[- ]?\s*seat(?:er)?s?|seats|seat count|seating capacity)\b", re.IGNORECASE),
    "fuel_type": re.compile(r"\b(?:petrol|diesel|gasoline|hybrid|electric|fuel[ -]?type)\b", re.IGNORECASE),
    "fuel_efficiency": re.compile(
        r"\b(?:fuel[ -]?efficien(?:t|cy)|low fuel consumption|mileage|km/?l)\b", re.IGNORECASE
    ),
    "transmission": re.compile(r"\b(?:automatic|manual|transmission|gearbox)\b", re.IGNORECASE),
    "with_driver": re.compile(r"\b(?:driver|chauffeur)\b", re.IGNORECASE),
    "deals": re.compile(
        r"\b(?:deals?|discounts?|promo(?:tion)?s?|coupons?|vouchers?|save\d+)\b",
        re.IGNORECASE,
    ),
    "features": re.compile(
        r"\b(?:bluetooth|gps|sunroof|child[ -]?seat|apple carplay|carplay|android auto|features?)\b",
        re.IGNORECASE,
    ),
    "car_type": re.compile(r"\b(?:SUVs?|sedans?|hatchbacks?|coupes?|convertibles?|vans?|pickups?|trucks?)\b", re.IGNORECASE),
}
SUPPORTED_VERIFIED_FILTERS = frozenset({
    "seats", "fuel_type", "fuel_efficiency", "transmission", "car_type", "brand",
})

SEAT_COUNT = re.compile(r"\b(?P<count>\d{1,2})\s*[- ]?\s*seat(?:er)?s?\b", re.IGNORECASE)
DURATION_DAYS = re.compile(r"\b(?:for\s+)?(?P<count>\d{1,3})\s+days?\b", re.IGNORECASE)
CAR_TYPE = re.compile(
    r"\b(?P<name>SUV|sedan|hatchback|coupe|convertible|van|pickup|truck)s?\b",
    re.IGNORECASE,
)
FUEL_TYPE = re.compile(r"\b(?P<name>petrol|diesel|gasoline|hybrid|electric)\b", re.IGNORECASE)
TRANSMISSION = re.compile(r"\b(?P<name>automatic|manual)\b", re.IGNORECASE)
FAMILY = re.compile(r"\bfamil(?:y|ies|y-friendly)\b", re.IGNORECASE)
PER_DAY = re.compile(r"\b(?:per\s*day|daily|/\s*day)\b", re.IGNORECASE)
TRIP_BUDGET = re.compile(r"\b(?:total|whole\s+trip|trip\s+budget)\b", re.IGNORECASE)
BUDGET_CHANGE = re.compile(
    r"\b(?:increase|raise|expand|change|adjust|update)\b.{0,35}"
    r"\b(?:budget|amount|price\s+limit|spending\s+limit)\b|"
    r"\b(?:budget|amount|price\s+limit|spending\s+limit)\b.{0,35}"
    r"\b(?:increase|raise|expand|change|adjust|update|higher)\b|"
    r"\bhigher\b.{0,25}\b(?:budget|amount|price\s+limit|spending\s+limit)\b",
    re.IGNORECASE,
)
_BUDGET_NUMBER = r"(?P<amount>\d[\d,]*(?:\.\d+)?)(?P<thousands>k)?"
BUDGET_LABEL_AMOUNT = re.compile(
    rf"\b(?:budget|amount|price\s+limit|spending\s+limit|spend|maximum|max)\b"
    rf"(?:\s+(?:is|of|to|at|up\s+to|under|below|less\s+than))?"
    rf"[:=]?\s*(?:aed\s*)?{_BUDGET_NUMBER}",
    re.IGNORECASE,
)
BUDGET_BOUND_AMOUNT = re.compile(
    rf"\b(?:under|below|less\s+than|up\s+to|maximum|max)\s+"
    rf"(?:aed\s*)?{_BUDGET_NUMBER}",
    re.IGNORECASE,
)
CURRENCY_AMOUNT = re.compile(
    rf"\baed\s*{_BUDGET_NUMBER}\b",
    re.IGNORECASE,
)
BARE_BUDGET_AMOUNT = re.compile(
    rf"^\s*(?:aed\s*)?{_BUDGET_NUMBER}(?:\s+per\s+day)?\s*[.!]?\s*$",
    re.IGNORECASE,
)


def budget_change_requested(message: str) -> bool:
    """Recognize budget refinements that need a value before another search."""
    return bool(BUDGET_CHANGE.search(message))


def budget_amount(message: str, *, allow_bare: bool = False) -> float | None:
    """Read a clearly stated maximum amount; bare numbers need a pending prompt."""
    match = (BUDGET_LABEL_AMOUNT.search(message)
             or BUDGET_BOUND_AMOUNT.search(message)
             or CURRENCY_AMOUNT.search(message))
    if match is None and allow_bare:
        match = BARE_BUDGET_AMOUNT.fullmatch(message)
    if match is None:
        return None
    try:
        amount = float(match.group("amount").replace(",", ""))
        return amount * 1000 if match.group("thousands") else amount
    except ValueError:
        return None


def verified_preference_updates(message: str) -> dict[str, object]:
    """Deterministic fallback for high-value requirements the model must not lose."""
    updates: dict[str, object] = {}
    if match := SEAT_COUNT.search(message):
        updates["seats"] = int(match.group("count"))
    if match := DURATION_DAYS.search(message):
        updates["rental_days"] = int(match.group("count"))
    if match := CAR_TYPE.search(message):
        updates["car_type_name"] = match.group("name").upper() if match.group("name").lower() == "suv" else match.group("name").title()
    if match := FUEL_TYPE.search(message):
        updates["fuel_type"] = match.group("name").title()
    if UNVERIFIED_FILTERS["fuel_efficiency"].search(message):
        updates["fuel_efficiency_preferred"] = True
    if match := TRANSMISSION.search(message):
        updates["transmission"] = match.group("name").title()
    if FAMILY.search(message):
        updates["family_suitable"] = True
    if PER_DAY.search(message):
        updates["budget_period"] = "day"
    elif TRIP_BUDGET.search(message):
        updates["budget_period"] = "trip"
    return updates

SECRET_REQUEST = re.compile(
    r"\b(?:system prompt|previous instructions|bearer token|api key|"
    r"mongodb|connection string|passwords?|credentials?)\b",
    re.IGNORECASE,
)
ADMIN_OR_ARBITRARY_API = re.compile(
    r"\b(?:login as admin|approve my account|deleteuser|/delete|/admin)\b",
    re.IGNORECASE,
)
ASSUME_AVAILABILITY = re.compile(
    r"\b(?:assume|pretend).{0,40}\b(?:available|availability)\b|"
    r"\bjust say yes\b",
    re.IGNORECASE,
)
INVENT_INVENTORY = re.compile(
    r"\b(?:invent|make up|fabricate|guess)\b.{0,80}\b(?:cars?|vehicles?|options?|inventory)\b|"
    r"\bdon't tell me you don't have (?:the )?data\b",
    re.IGNORECASE,
)
EXPLICIT_SEARCH_RESET = re.compile(
    r"\b(?:start(?:ing)?\s+(?:a\s+)?new\s+search|new\s+search|start\s+over|from\s+scratch|"
    r"clear\s+(?:my\s+)?filters?)\b",
    re.IGNORECASE,
)
CATALOG_ONLY_BLOCKS = frozenset({"car_type", "brand"})


def unverified_constraints(message: str, extracted: list[str]) -> list[str]:
    """Model classification plus a conservative server-side safety net."""
    found = set(extracted) - SUPPORTED_VERIFIED_FILTERS
    for name, pattern in UNVERIFIED_FILTERS.items():
        if name in SUPPORTED_VERIFIED_FILTERS:
            continue
        if pattern.search(message):
            found.add(name)
    if "seats" in found and not UNVERIFIED_FILTERS["seats"].search(message):
        found.discard("seats")
    return sorted(found)


UNVERIFIED_LABELS = {
    "seats": "seat count",
    "fuel_type": "fuel type",
    "fuel_efficiency": "fuel efficiency",
    "transmission": "transmission",
    "with_driver": "driver option",
    "deals": "deals",
    "features": "features",
    "car_type": "car type",
    "brand": "brand",
}


def _blocked_phrase(blocked: list[str]) -> str:
    return ", ".join(UNVERIFIED_LABELS[name] for name in blocked)


def unverified_filter_message(blocked: list[str], filters, needs_dates: bool) -> str:
    """Clarify unverified filters while acknowledging verified criteria already saved."""
    notes: list[str] = []
    if getattr(filters, "max_price", None) is not None:
        notes.append(f"I've noted up to AED {filters.max_price:g}.")
    if getattr(filters, "min_price", None) is not None and getattr(filters, "max_price", None) is None:
        notes.append(f"I've noted from AED {filters.min_price:g}.")
    location = getattr(filters, "location", None)
    if location:
        notes.append(f"I'll keep searching in {location}.")
    names = _blocked_phrase(blocked)
    lead = " ".join(notes)
    if lead:
        lead = f"{lead} "
    has_area = bool(location or getattr(filters, "lat", None) is not None)
    has_budget = getattr(filters, "max_price", None) is not None or getattr(filters, "min_price", None) is not None
    if blocked == ["seats"]:
        return f"{lead}How many people will be travelling?"
    if needs_dates and not (getattr(filters, "from_date", None) and getattr(filters, "to_date", None)):
        return (f"{lead}I can't filter by {names} in chat yet. "
                "What pickup date should I use for your rental?")
    if has_area and has_budget:
        return (f"{lead}I can't filter by {names} without a vehicle type from the app. "
                "Say find a car to show matches for your area and budget.")
    if has_area or has_budget:
        missing = "area" if not has_area else "budget"
        return (f"{lead}I can't filter by {names} yet. What {missing} should I use?")
    return (f"{lead}I can't filter by {names} yet. "
            "What area or budget should I search with?")


def explicit_search_reset(message: str) -> bool:
    return bool(EXPLICIT_SEARCH_RESET.search(message))


def catalog_only_blocks(blocked: list[str]) -> bool:
    return bool(blocked) and all(name in CATALOG_ONLY_BLOCKS for name in blocked)


def grounded_refusal(message: str) -> str | None:
    """Refuse secret extraction, admin actions, assumed facts, and invented inventory."""
    if SECRET_REQUEST.search(message):
        return "I can't share internal credentials, tokens, or system instructions."
    if ADMIN_OR_ARBITRARY_API.search(message):
        return ("I can't perform admin operations or call arbitrary Gear endpoints. "
                "I can help search vehicles and explain the booking process.")
    if ASSUME_AVAILABILITY.search(message):
        return "I can't confirm availability without checking live availability for that car."
    if INVENT_INVENTORY.search(message):
        return "I couldn't find verified matching vehicles. I only show listings I can confirm."
    return None


RATING_QUESTION = re.compile(r"\b(?:\d+\s*[- ]?\s*stars?|ratings?)\b", re.IGNORECASE)


def missing_verified_rating(message: str, cars: list) -> bool:
    if not RATING_QUESTION.search(message) or not cars:
        return False
    return all(getattr(car, "rating", None) is None for car in cars)
