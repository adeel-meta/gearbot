"""User-facing copy for the Gear rental concierge (no backend jargon)."""

from __future__ import annotations

from datetime import date, timedelta

from app.schemas.chat import SearchFilters, VehicleCard
from app.services.dates import gear_today
from app.services.dialogue_policy import dates_description, no_results_actions


OPENING_SUGGESTIONS = [
    "Find a car",
    "Cars near me",
    "Family trip",
    "Budget cars",
    "How booking works",
]


def opening_message() -> str:
    return (
        "Hey — I'm here to help you find the right rental. Tell me what you need: "
        "a location, dates, budget, or even just the kind of trip you're planning."
    )


def near_me_prompt() -> str:
    return "Share your location or tell me the area you'd like to search."


def location_prompt(filters: SearchFilters | None = None) -> str:
    dates = dates_description(filters) if filters is not None else None
    if dates:
        return f"{dates}, got it. Where should I search? Which city or area?"
    return "Where should I search? Tell me a city or area."


def dates_prompt(pickup_only: bool = False, return_only: bool = False,
                 today: date | None = None) -> str:
    if return_only:
        return "What return date should I use?"
    if pickup_only:
        return "What date would you like to start the rental?"
    pickup = (today or gear_today()) + timedelta(days=7)
    dropoff = pickup + timedelta(days=2)
    example = (f"{pickup.day} to {dropoff.day} {pickup.strftime('%B')}"
               if pickup.month == dropoff.month else
               f"{pickup.day} {pickup.strftime('%B')} to "
               f"{dropoff.day} {dropoff.strftime('%B')}")
    return f"What pickup and return dates should I use? For example: {example}."


def budget_amount_prompt() -> str:
    return "What maximum budget should I use? For example, AED 800 per day."


def empty_search_prompt() -> str:
    return opening_message()


def booking_guidance_message() -> str:
    return (
        "I can help you find a car and check availability here. To book, sign in to the "
        "Gear app, select the car, review its price and rental terms, and complete the "
        "booking there."
    )


def booking_guidance_suggestions() -> list[str]:
    return ["Find a car", "Cancellation policy", "What documents do I need?"]


def _budget_phrase(filters: SearchFilters) -> str | None:
    if filters.max_price is not None and filters.min_price is not None:
        return f"AED {filters.min_price:g}–{filters.max_price:g}"
    if filters.max_price is not None:
        return f"up to AED {filters.max_price:g}"
    if filters.min_price is not None:
        return f"from AED {filters.min_price:g}"
    return None


def criteria_summary(filters: SearchFilters, *, lead: str = "Got it") -> str:
    bits: list[str] = []
    if filters.location:
        bits.append(filters.location)
    if filters.car_type_name:
        bits.append(filters.car_type_name)
    if filters.seats:
        bits.append(f"at least {filters.seats} seats")
    budget = _budget_phrase(filters)
    if budget:
        bits.append(budget)
    if filters.from_date and filters.to_date:
        bits.append(f"{filters.from_date.isoformat()} to {filters.to_date.isoformat()}")
    if not bits:
        return lead + "."
    return f"{lead} — {', '.join(bits)}."


def budget_change_acknowledgement(
    before: SearchFilters, after: SearchFilters,
) -> str | None:
    if before.max_price == after.max_price:
        return None
    if after.max_price is None:
        return None
    location = after.location or before.location
    loc = f" in {location}" if location else ""
    if before.max_price is not None and after.max_price > before.max_price:
        return f"Done — I've expanded your budget to AED {after.max_price:g}{loc}."
    if before.max_price is not None and after.max_price < before.max_price:
        return f"Done — I've lowered your budget to AED {after.max_price:g}{loc}."
    return f"Done — I'll search with a budget up to AED {after.max_price:g}{loc}."


def results_message(
    count: int,
    filters: SearchFilters,
    *,
    catalog_skipped: bool = False,
    refinement_note: str | None = None,
) -> str:
    if refinement_note:
        noun = "car" if count == 1 else "cars"
        message = f"{refinement_note.rstrip('.')}. I found {count} matching {noun}."
        if filters.from_date and filters.to_date:
            message = f"{message.rstrip('.')} for {dates_description(filters)}."
        if catalog_skipped:
            message += (
                " I couldn't narrow these by vehicle type from the app yet, "
                "so these match your other preferences."
            )
        return message
    elif count == 1:
        lead = "I found 1 car"
    else:
        lead = f"I found {count} cars"
    location = filters.location
    budget = _budget_phrase(filters)
    if location and budget:
        tail = f" in {location} within your {budget.replace('up to ', '')} budget."
    elif location:
        tail = f" in {location}."
    elif budget:
        tail = f" within your {budget.replace('up to ', '')} budget."
    else:
        tail = " that match what you asked for."
    message = lead + tail
    if filters.from_date and filters.to_date:
        message = f"{message.rstrip('.')} for {dates_description(filters)}."
    if catalog_skipped:
        message += (
            " I couldn't narrow these by vehicle type from the app yet, "
            "so these match your other preferences."
        )
    return message


def no_results_message(filters: SearchFilters) -> str:
    summary = criteria_summary(filters, lead="I searched")
    return (
        f"{summary.rstrip('.')} but couldn't find a verified match for those filters right now. "
        "I can broaden the search if you'd like."
    )


def no_results_suggestions(filters: SearchFilters | None = None,
                           *, anywhere: bool = False,
                           trip_budget: bool = False) -> list[str]:
    return no_results_actions(filters or SearchFilters(), anywhere=anywhere,
                              trip_budget=trip_budget)


def results_suggestions(filters: SearchFilters, *, has_single_result: bool = False) -> list[str]:
    chips = ["Cheapest first", "Change dates",
             "Change budget" if filters.max_price is not None else "Set budget",
             "Different vehicle type"]
    if has_single_result:
        return ["Check availability", "Price details", "Different vehicle type", "Booking help"]
    if filters.sort_by == "price_low":
        chips[0] = "Recommended order"
    return chips


def unavailable_vehicle_message() -> str:
    return (
        "That one isn't available for your selected dates. "
        "I can show you similar cars that are."
    )


def unavailable_vehicle_suggestions() -> list[str]:
    return ["Different vehicle type", "Change dates"]


def vehicle_match_reason(card: VehicleCard, filters: SearchFilters) -> str | None:
    reasons: list[str] = []
    budget = filters.max_price
    if budget is not None and card.price is not None and card.price <= budget:
        reasons.append("within your budget")
    if filters.min_rating is not None and card.rating is not None:
        if card.rating >= filters.min_rating:
            reasons.append("meets your rating preference")
    if filters.featured and card.is_featured:
        reasons.append("featured listing")
    if filters.car_type_name and card.car_type_name:
        if filters.car_type_name.casefold() == card.car_type_name.casefold():
            reasons.append(f"verified {card.car_type_name} type")
    if filters.seats is not None and card.seats is not None and card.seats >= filters.seats:
        reasons.append(f"has {card.seats} seats")
    if filters.fuel_type and card.fuel_type:
        if filters.fuel_type.casefold() == card.fuel_type.casefold():
            reasons.append(f"uses {card.fuel_type}")
    if filters.fuel_efficiency_preferred:
        if card.fuel_efficiency is not None:
            reasons.append(f"reports {card.fuel_efficiency:g} fuel-efficiency mileage")
        elif card.is_fuel_efficient is True:
            reasons.append("marked fuel-efficient by Gear")
    if filters.transmission and card.transmission:
        if filters.transmission.casefold() == card.transmission.casefold():
            reasons.append(f"has {card.transmission} transmission")
    if filters.family_suitable:
        if card.is_family_suitable is True:
            reasons.append("marked family-suitable by Gear")
        elif card.seats is not None and card.seats >= 5:
            reasons.append("has verified family-capacity seating")
    if not reasons:
        return None
    if len(reasons) == 1:
        return f"Why it fits: {reasons[0].capitalize()}."
    joined = ", ".join(reasons[:-1]) + f", and {reasons[-1]}"
    message = f"Why it fits: {joined.capitalize()}."
    return message if len(message) <= 240 else message[:237].rstrip(" ,") + "..."


def with_match_reasons(cars: list[VehicleCard], filters: SearchFilters) -> list[VehicleCard]:
    enriched: list[VehicleCard] = []
    for car in cars:
        reason = vehicle_match_reason(car, filters)
        if reason and car.match_reason is None:
            enriched.append(car.model_copy(update={"match_reason": reason}))
        else:
            enriched.append(car)
    return enriched
