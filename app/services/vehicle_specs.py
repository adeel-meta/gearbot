"""Questions about a focused vehicle's verified listing attributes."""

import re
from typing import Literal

from app.schemas.chat import VehicleCard

SpecTopic = Literal["seats", "mileage", "fuel", "transmission", "luggage", "overview"]

GREETING_LEAD = re.compile(
    r"^(?:good\s+(?:morning|afternoon|evening)|assalamu?\s+alaikum|salam|hello|hey|hi)"
    r"(?:\s+there)?(?=$|[\s,!.])",
    re.IGNORECASE,
)
FOCUS_VEHICLE = re.compile(
    r"\b(?:this|that|the)\s+car\b|\boption\s+\d+\b|\b(?:it|one)\b",
    re.IGNORECASE,
)
SEATS_QUESTION = re.compile(
    r"\b(?:how many|number of|count of).{0,40}\bseat|\bseat(?:ing)?\s+capacity\b|"
    r"\bseats?\s+does\b",
    re.IGNORECASE,
)
MILEAGE_QUESTION = re.compile(
    r"\b(?:mileage|fuel[ -]?efficien(?:t|cy)|km/?l|kilometers? per litre)\b",
    re.IGNORECASE,
)
FUEL_QUESTION = re.compile(r"\b(?:what|which)\s+fuel\b|\bfuel\s+type\b", re.IGNORECASE)
TRANSMISSION_QUESTION = re.compile(
    r"\b(?:automatic|manual)\b.*\?|\bwhat\s+transmission\b|\btransmission\s+type\b",
    re.IGNORECASE,
)
LUGGAGE_QUESTION = re.compile(
    r"\b(?:luggage|boot|trunk|baggage)\s+(?:space|capacity)\b",
    re.IGNORECASE,
)


def split_greeting(message: str) -> str | None:
    """Text after a leading greeting, or None when the message does not start with one.

    An empty string means the message is only a greeting.
    """
    text = message.strip()
    match = GREETING_LEAD.match(text)
    if not match:
        return None
    return text[match.end():].strip(" \t,!.?-–—")


def is_greeting(message: str) -> bool:
    return split_greeting(message) == ""


def vehicle_spec_topic(message: str) -> SpecTopic | None:
    if SEATS_QUESTION.search(message):
        return "seats"
    if MILEAGE_QUESTION.search(message):
        return "mileage"
    if FUEL_QUESTION.search(message):
        return "fuel"
    if TRANSMISSION_QUESTION.search(message):
        return "transmission"
    if LUGGAGE_QUESTION.search(message):
        return "luggage"
    if re.search(r"\b(?:spec(?:s|ification)?s?)\b", message, re.IGNORECASE):
        return "overview"
    return None


def asks_about_focus_vehicle(message: str, *, has_selected: bool) -> bool:
    if FOCUS_VEHICLE.search(message):
        return True
    if has_selected and vehicle_spec_topic(message) is not None:
        return True
    return False


def vehicle_spec_message(card: VehicleCard, topic: SpecTopic) -> str:
    title = card.title or "This car"
    if topic == "seats":
        if card.seats is not None:
            label = "seat" if card.seats == 1 else "seats"
            return f"{title} has {card.seats} {label} in Gear's verified listing."
        return f"I don't have a verified seat count for {title} in Gear's listing."
    if topic == "mileage":
        if card.fuel_efficiency is not None:
            return f"{title} reports {card.fuel_efficiency:g} fuel-efficiency mileage in Gear's listing."
        if card.is_fuel_efficient is True:
            return f"{title} is marked fuel-efficient in Gear's listing, but no mileage figure is listed."
        return f"I don't have verified mileage or fuel-efficiency data for {title}."
    if topic == "fuel":
        if card.fuel_type:
            return f"{title} uses {card.fuel_type} according to Gear's listing."
        return f"I don't have a verified fuel type for {title}."
    if topic == "transmission":
        if card.transmission:
            return f"{title} has {card.transmission} transmission in Gear's listing."
        return f"I don't have verified transmission data for {title}."
    if topic == "luggage":
        if card.luggage_capacity is not None:
            return f"{title} lists luggage capacity for {card.luggage_capacity} bags in Gear."
        return f"I don't have verified luggage capacity for {title}."
    lines: list[str] = []
    if card.seats is not None:
        lines.append(f"{card.seats} seats")
    if card.fuel_type:
        lines.append(card.fuel_type)
    if card.transmission:
        lines.append(f"{card.transmission} transmission")
    if card.fuel_efficiency is not None:
        lines.append(f"{card.fuel_efficiency:g} fuel-efficiency mileage")
    elif card.is_fuel_efficient is True:
        lines.append("marked fuel-efficient")
    if card.luggage_capacity is not None:
        lines.append(f"luggage for {card.luggage_capacity} bags")
    if not lines:
        return f"I don't have verified specification details for {title} in Gear's listing yet."
    return f"From Gear's verified listing, {title}: " + ", ".join(lines) + "."
