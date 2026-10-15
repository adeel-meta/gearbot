"""Conversation requirements and local actions for the rental search task."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from app.schemas.chat import SearchFilters


Question = Literal["location", "dates", "budget", "budget_period", "vehicle_type"]
ContextField = Literal["location", "dates", "budget", "vehicle_type", "summary", "reason"]


def normalized(message: str) -> str:
    return " ".join(message.casefold().strip(" \t\n.!?").split())


def starts_guided_search(message: str) -> bool:
    value = normalized(message)
    return (value in {"family trip", "budget cars", "cars near me"}
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
    if not (value.startswith(("what ", "which ", "where ", "why ", "did i ",
                              "have i ", "do you ", "are you "))):
        return None
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
    if value.startswith("why "):
        return "reason"
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
        "keep the old one", "leave it as it was",
    }


def next_question(mode: str, filters: SearchFilters, *, anywhere: bool,
                  budget_decided: bool) -> Question | None:
    """Application rules decide readiness; extractor hints cannot waive a requirement."""
    if not (filters.location or filters.lat is not None or anywhere):
        return "location"
    if mode == "guided":
        if not (filters.from_date and filters.to_date):
            return "dates"
        if not budget_decided and filters.max_price is None and filters.min_price is None:
            return "budget"
    return None


def no_results_actions(filters: SearchFilters, *, anywhere: bool,
                       trip_budget: bool = False) -> list[str]:
    actions: list[str] = []
    if filters.max_price is not None:
        actions.append("Increase budget")
    elif trip_budget:
        actions.append("Set daily budget")
    elif filters.min_price is None:
        actions.append("Set budget")
    if filters.location or filters.lat is not None:
        actions.append("Change area")
    elif not anywhere:
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
