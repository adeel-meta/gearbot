"""Conversation requirements and local actions for the rental search task."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from app.schemas.chat import SearchFilters


Question = Literal["location", "dates", "budget", "budget_period", "vehicle_type"]
ContextField = Literal["location", "dates", "budget", "vehicle_type", "summary", "reason"]
TurnAction = Literal[
    "supply", "revise", "recall", "policy", "booking", "result", "reset", "unclear", "search",
]
TurnTarget = Literal["location", "dates", "budget", "vehicle_type", "booking", "results"]

_PLACE_ALIASES = {
    "duabi": "Dubai",
    "dubia": "Dubai",
    "dubaii": "Dubai",
    "dubai": "Dubai",
    "sharja": "Sharjah",
    "sharjah": "Sharjah",
    "abu dabi": "Abu Dhabi",
    "abu dhabi": "Abu Dhabi",
    "ajman": "Ajman",
}
_LEADING_PLACE = re.compile(r"^(?:in|at|near|around)\s+", re.IGNORECASE)
_CHANGE_VERB = re.compile(r"\b(?:change|update|switch|edit|replace|correct)\b")
_LOCATION_FIELD = re.compile(r"\b(?:location|area|city|place)\b")
_DATE_FIELD = re.compile(r"\b(?:dates?|schedule)\b")
_BUDGET_FIELD = re.compile(r"\b(?:budget|price limit|spending limit)\b")
_TYPE_FIELD = re.compile(r"\b(?:vehicle type|body type|car type)\b")
_KNOWN_TYPE = re.compile(
    r"\b(?:suv|sedan|hatchback|coupe|convertible|van|pickup|truck)s?\b",
)
_REPLACEMENT_PLACE = re.compile(
    r"\b(?:to|as)\s+([A-Za-z][A-Za-z '\-]{1,40})\s*$",
)
_ONE_AMOUNT = re.compile(r"\b(\d[\d,]*(?:\.\d+)?)\b")
_MONEY_CONTEXT = re.compile(
    r"\b(?:aed|budget|per\s*day|daily|/\s*day|like|around|about)\b",
    re.IGNORECASE,
)


def normalize_place(text: str) -> str:
    """Store a place without a leading preposition, using a known city spelling."""
    value = _LEADING_PLACE.sub("", " ".join(text.strip().split()))
    alias = _PLACE_ALIASES.get(value.casefold())
    return alias if alias is not None else value.strip(" .")


def place_matches(wanted: str, card_location: str) -> bool:
    """Match a saved city to a Gear label such as 'Dubai - AE'."""
    wanted_key = normalize_place(wanted).casefold()
    card = card_location.casefold()
    city = re.split(r"\s+-\s+", card, maxsplit=1)[0].strip()
    return wanted_key == city or wanted_key in card


def stated_budget_amount(message: str) -> float | None:
    """Read one amount from a budget reply such as 'like 250 per day'."""
    if _MONEY_CONTEXT.search(message) is None:
        return None
    numbers = _ONE_AMOUNT.findall(message)
    if len(numbers) != 1:
        return None
    try:
        return float(numbers[0].replace(",", ""))
    except ValueError:
        return None


def classify_turn(message: str) -> tuple[TurnAction, TurnTarget | None] | None:
    """Decide a turn when its structure is clear. Paraphrases stay with the extractor."""
    value = normalized(message)
    if value in {"change it", "change something", "something else", "change requirements"}:
        return "unclear", None
    if value in {"start over", "clear everything", "clear filters", "reset"}:
        return "reset", None
    if re.search(r"\b(?:book this|my booking|where is my booking)\b", value):
        return "booking", "booking"
    if re.search(r"\bpolicy\b", value) and re.search(
        r"\b(?:cancel|cancell?ation|refund|document|deposit|fee|support)\b", value,
    ):
        return "policy", None
    if _KNOWN_TYPE.search(value) and re.search(r"\b(?:make it|switch to|change to)\b", value):
        return "revise", "vehicle_type"
    fields: list[TurnTarget] = []
    if _LOCATION_FIELD.search(value):
        fields.append("location")
    if _DATE_FIELD.search(value):
        fields.append("dates")
    if _BUDGET_FIELD.search(value):
        fields.append("budget")
    if _TYPE_FIELD.search(value):
        fields.append("vehicle_type")
    wants_change = _CHANGE_VERB.search(value) is not None
    if not wants_change or not fields:
        return None
    if len(fields) != 1:
        return "unclear", None
    return "revise", fields[0]


def revision_place(message: str) -> str | None:
    match = _REPLACEMENT_PLACE.search(message.strip())
    if match is None:
        return None
    place = normalize_place(match.group(1))
    if _CHANGE_VERB.search(place) or _LOCATION_FIELD.search(place) or _DATE_FIELD.search(place):
        return None
    return place


def revision_vehicle_type(message: str) -> str | None:
    match = _KNOWN_TYPE.search(message)
    if match is None:
        return None
    name = match.group(0).casefold().rstrip("s")
    if name == "suv":
        return "SUV"
    return name.title()


def normalized(message: str) -> str:
    return " ".join(message.casefold().strip(" \t\n.!?").split())


def near_me_request(message: str) -> bool:
    return normalized(message) in {
        "cars near me", "show cars near me", "show me cars near me",
    }


def starts_guided_search(message: str) -> bool:
    value = normalized(message)
    return (value in {"family trip", "budget cars", "cheap car", "cheap cars", "cars near me"}
            or near_me_request(message)
            or value == "find a car" or value.startswith(("find a car ", "i need a car ")))


def starts_browsing(message: str) -> bool:
    value = normalized(message)
    return value in {"just browsing", "browse cars", "show cars", "show me cars",
                     "keep browsing"}


def explicit_anywhere(message: str) -> bool:
    return normalized(message) in {"anywhere", "any area", "all areas", "any location"}


def skips_budget(message: str) -> bool:
    return normalized(message) in {
        "no budget", "no limit", "any budget", "skip budget",
        "show options without a limit", "show cars now", "show options",
    }


def context_question(message: str) -> ContextField | None:
    value = normalized(message)
    if value == "location" and message.strip().endswith("?"):
        return "location"
    value = re.sub(r"\bwat\b", "what", value)
    value = re.sub(r"\bwhre\b", "where", value)
    value = re.sub(r"\bwhyy\b", "why", value)
    value = re.sub(r"^(?:yes|yeah|yep|ok|okay)[, ]+", "", value)
    value = re.sub(r"\bu\b", "you", value)
    if not (value.startswith(("what ", "which ", "where ", "why ", "did i ",
                              "have i ", "do you ", "are you "))
            or value in {"why dates", "why area", "why location", "why budget"}):
        return None
    if value.startswith("why "):
        return "reason"
    if re.search(r"\b(?:area|location|city|place)\b", value):
        return "location"
    if value.startswith(("did i say ", "did i mention ", "have i said ")):
        if re.search(r"\b(?:aed|budget)\b", value):
            return "budget"
        if re.search(r"\b(?:\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b", value):
            return "dates"
        return "location"
    if re.search(r"\b(?:date|dates|when|day|days)\b", value):
        return "dates"
    if re.search(r"\b(?:budget|price limit|spending limit)\b", value):
        return "budget"
    if re.search(r"\b(?:type|kind|suv|sedan)\b", value):
        return "vehicle_type"
    if re.search(r"\b(?:searching|looking|filters|requirements)\b", value):
        return "summary"
    return None


def change_request(message: str) -> Question | None:
    value = normalized(message)
    if value in {"increase budget", "change budget", "set budget", "raise budget",
                 "set daily budget"}:
        return "budget"
    if value in {"change dates", "different dates", "new dates"}:
        return "dates"
    if value in {"nearby areas", "change area", "different area", "change location"}:
        return "location"
    if value in {"different vehicle type", "change vehicle type", "choose vehicle type"}:
        return "vehicle_type"
    return None


def clear_request(message: str) -> str | None:
    value = normalized(message)
    if re.fullmatch(r"(?:remove|clear|drop|forget) (?:the |my )?(?:budget|price limit)", value):
        return "budget"
    if value in {"any vehicle type", "any type", "no type preference", "remove vehicle type"}:
        return "vehicle_type"
    return None


def corrected_location(message: str) -> str | None:
    match = re.fullmatch(
        r"\s*(?:actually|no[,]?|instead[,]?)\s+([A-Za-z][A-Za-z '\-]{1,70})[.!]?\s*",
        message, re.IGNORECASE,
    )
    return match.group(1).strip() if match else None


def cancels_pending_change(message: str) -> bool:
    return normalized(message) in {
        "cancel that change", "cancel this change", "never mind", "nevermind",
        "keep the old one", "leave it as it was", "cancelation", "cancellation",
    }


def next_question(mode: str, filters: SearchFilters, *, anywhere: bool,
                  budget_decided: bool, location_locked: bool = False) -> Question | None:
    """Application rules decide readiness; extractor hints cannot waive a requirement."""
    if not location_locked and not (filters.location or filters.lat is not None or anywhere):
        return "location"
    if mode == "guided":
        if not (filters.from_date and filters.to_date):
            return "dates"
        if not budget_decided and filters.max_price is None and filters.min_price is None:
            return "budget"
    return None


def no_results_actions(filters: SearchFilters, *, anywhere: bool,
                       trip_budget: bool = False, location_locked: bool = False) -> list[str]:
    actions: list[str] = []
    if filters.max_price is not None:
        actions.append("Increase budget")
    elif trip_budget:
        actions.append("Set daily budget")
    elif filters.min_price is None:
        actions.append("Set budget")
    if filters.location or filters.lat is not None:
        actions.append("Change area")
    elif not anywhere and not location_locked:
        actions.append("Choose area")
    if filters.car_type_id or filters.car_type_name or filters.search:
        actions.append("Different vehicle type")
    if filters.from_date and filters.to_date:
        actions.append("Change dates")
    return actions


def dates_description(filters: SearchFilters) -> str | None:
    if filters.from_date and filters.to_date:
        start: date = filters.from_date
        end: date = filters.to_date
        if start.year == end.year and start.month == end.month:
            return f"{start.day}–{end.day} {start.strftime('%B')} {start.year}"
        return f"{start.isoformat()} to {end.isoformat()}"
    if filters.from_date:
        return f"pickup {filters.from_date.isoformat()}"
    if filters.to_date:
        return f"return {filters.to_date.isoformat()}"
    return None
