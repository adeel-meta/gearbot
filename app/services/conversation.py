"""Deterministic chat flow around the model and Gear API boundaries."""

import asyncio
from datetime import date, timedelta
import logging
import re
from typing import Callable

from pydantic import ValidationError

from app.clients.gear import (
    GearAuthorizationRequired,
    GearClient,
    GearUnavailable,
    resolve_catalog_id,
)
from app.clients.llm import Extraction, LLMUnavailable, RequirementExtractor
from app.config import ConfigurationError
from app.guardrails.policy import (
    budget_amount,
    budget_change_requested,
    catalog_only_blocks,
    explicit_search_reset,
    grounded_refusal,
    missing_verified_rating,
    unverified_constraints,
    unverified_filter_message,
    verified_preference_updates,
)
from app.schemas.chat import ChatAction, ChatRequest, ChatResponse, SearchFilters, VehicleCard
from app.security.sessions import PendingProtectedOperation, Session, SessionStore
from app.services.dates import gear_today, has_bare_day_range, resolve_month_day_range
from app.services.dialogue_policy import (
    cancels_pending_change,
    change_request,
    clear_request,
    context_question,
    corrected_location,
    dates_description,
    explicit_anywhere,
    next_question,
    normalized,
    skips_budget,
    starts_browsing,
    starts_guided_search,
)
from app.services.faqs import faq_answer
from app.services.messaging import (
    OPENING_SUGGESTIONS,
    booking_guidance_message,
    booking_guidance_suggestions,
    budget_amount_prompt,
    budget_change_acknowledgement,
    criteria_summary,
    dates_prompt,
    empty_search_prompt,
    location_prompt,
    near_me_prompt,
    no_results_message,
    no_results_suggestions,
    opening_message,
    results_message,
    results_suggestions,
    unavailable_vehicle_message,
    unavailable_vehicle_suggestions,
    with_match_reasons,
)
from app.services.vehicle_specs import (
    SpecTopic,
    asks_about_focus_vehicle,
    is_greeting,
    vehicle_spec_message,
    vehicle_spec_topic,
)


logger = logging.getLogger("gear_ai.conversation")


class TurnConflict(Exception):
    """A client reused a turn ID for a different message."""


class ConversationService:
    def __init__(self, extractor: RequirementExtractor, gear: GearClient,
                 sessions: SessionStore, data_source: str = "gear",
                 today: Callable[[], date] = gear_today):
        self.extractor = extractor
        self.gear = gear
        self.sessions = sessions
        self.data_source = data_source
        self.today = today
        self._turn_locks: dict[object, asyncio.Lock] = {}

    async def chat(self, request: ChatRequest, token: str | None) -> ChatResponse:
        session = self.sessions.get_or_create(request.session_id)
        lock = self._turn_locks.setdefault(session.id, asyncio.Lock())
        async with lock:
            lease = await self.sessions.acquire_turn(session.public_token)
            try:
                # Reload after the distributed lease: another worker may have completed a turn.
                session = self.sessions.get_or_create(session.public_token)
                if request.turn_id and request.turn_id in session.recent_responses:
                    previous_message, previous_response = session.recent_responses[request.turn_id]
                    if previous_message != request.message:
                        raise TurnConflict("Turn ID reused for a different message")
                    if not (previous_response.status == "authentication_required" and token):
                        return previous_response.model_copy(deep=True)
                response = await self._chat_turn(request, token, session)
                if request.turn_id:
                    session.recent_responses[request.turn_id] = (
                        request.message, response.model_copy(deep=True),
                    )
                    while len(session.recent_responses) > 32:
                        session.recent_responses.pop(next(iter(session.recent_responses)))
                self.sessions.assert_turn_lease(session.public_token, lease)
                self.sessions.save(session)
                logger.info(
                    "turn_decision status=%s action=%s pending=%s revision=%s outcome=%s",
                    response.status, session.last_assistant_action,
                    "budget" if session.pending_budget else session.pending_requirement,
                    session.criteria_revision, session.last_search_outcome,
                )
                return response
            finally:
                await self.sessions.release_turn(session.public_token, lease)

    async def _chat_turn(self, request: ChatRequest, token: str | None,
                         session: Session) -> ChatResponse:
        session.recent_turns = (session.recent_turns + [request.message])[-4:]
        if (session.pending_operation and token
                and (self._automatic_resume_message(request.message)
                     or self._message_matches_pending_operation(
                         request.message, session.pending_operation))
                and not explicit_search_reset(request.message)):
            return await self._resume_pending_operation(session, token)
        if local_response := await self._handle_local_turn(session, request, token):
            return local_response
        if faq := faq_answer(request.message):
            return self._response(session, "guidance", faq[0], suggestions=faq[1])
        if is_greeting(request.message):
            return self._response(
                session, "guidance", opening_message(), suggestions=OPENING_SUGGESTIONS,
            )
        filters_before = session.filters.model_copy(deep=True)
        result_reference_was_current = (
            session.results_revision is not None
            and session.results_revision == session.criteria_revision
        )
        continuing_selected_availability = session.pending_vehicle_intent == "availability"
        extracted = self._fallback_extraction(request, session)
        if extracted is not None:
            logger.info("Used deterministic fallback for a date or budget clarification")
        else:
            try:
                titles = [card.title or f"Option {index}"
                          for index, card in enumerate(session.results, start=1)]
                contextual_extract = getattr(self.extractor, "extract_with_context", None)
                if callable(contextual_extract):
                    extracted = await contextual_extract(
                        request.message, session.filters, titles,
                        self._extraction_context(session),
                    )
                else:
                    extracted = await self.extractor.extract(
                        request.message, session.filters, titles,
                    )
            except (LLMUnavailable, ConfigurationError) as exc:
                logger.warning(
                    "Requirement extraction unavailable; no deterministic fallback "
                    "(error_type=%s)", type(exc).__name__,
                )
                return self._response(
                    session, "unavailable",
                    "I can't process your request right now. Please try again shortly.",
                )

        spec_topic = vehicle_spec_topic(request.message)
        if extracted.intent not in {"details", "availability", "pricing"} and (
            extracted.intent == "vehicle_specs"
            or (
                spec_topic
                and asks_about_focus_vehicle(
                    request.message, has_selected=session.selected_vehicle_id is not None,
                )
            )
        ):
            topic: SpecTopic = spec_topic or "overview"
            card = self._resolve_focus_card(session, extracted.selected_index)
            if card is None:
                return self._response(
                    session, "clarification",
                    "Which car should I check? Pick an option from your results.",
                )
            return await self._vehicle_spec_answer(session, topic, card, token)

        if extracted.intent == "context_question":
            return self._context_answer(session, extracted.question_field or "summary")
        if extracted.intent == "unsupported":
            return self._response(session, "guidance",
                                  "I can help you find a rental and explain how booking works.")
        if extracted.intent == "booking_guidance":
            return self._response(
                session, "guidance", booking_guidance_message(),
                suggestions=booking_guidance_suggestions(),
                action=ChatAction(type="SIGN_IN"),
            )
        refusal = grounded_refusal(request.message)
        if refusal:
            return self._response(session, "guidance", refusal)

        merge_error = self._merge_extracted_filters(session, request, extracted)
        if merge_error:
            return merge_error

        period_fields = {
            "from_date", "to_date", "from_time", "to_time", "rental_days",
            "min_price", "max_price", "budget_period",
        }
        changed_other_criteria = any(
            getattr(filters_before, name) != getattr(session.filters, name)
            for name in SearchFilters.model_fields if name not in period_fields
        )
        allow_selected_car_for_this_turn = (
            (result_reference_was_current or continuing_selected_availability)
            and not changed_other_criteria
        )

        if extracted.intent not in {"details", "pricing"}:
            date_error = self._past_pickup_error(session)
            if date_error:
                return date_error

        budget_ack = budget_change_acknowledgement(filters_before, session.filters)

        if session.pending_vehicle_intent == "availability" and extracted.intent == "search":
            return await self._vehicle_follow_up(
                session, "availability", None, token,
                allow_stale=allow_selected_car_for_this_turn,
            )

        blocked = unverified_constraints(request.message, extracted.unsupported_constraints)
        if self.data_source == "demo":
            blocked = [name for name in blocked if name not in {"car_type", "brand"}]
        if request.context.car_type_id or request.context.car_type_ids:
            blocked = [name for name in blocked if name != "car_type"]
        if request.context.brand_id:
            blocked = [name for name in blocked if name != "brand"]
        catalog_skipped = False
        if blocked:
            searchable = bool(session.filters.query_params())
            needs_missing_dates = (
                extracted.needs_dates
                and not (session.filters.from_date and session.filters.to_date)
            )
            has_budget = (
                session.filters.max_price is not None or session.filters.min_price is not None
            )
            refined_search = bool(session.results) or has_budget
            if (catalog_only_blocks(blocked) and searchable and not needs_missing_dates
                    and refined_search):
                blocked = []
                catalog_skipped = True
            else:
                if needs_missing_dates:
                    session.pending_requirement = "dates"
                return self._response(
                    session, "clarification",
                    unverified_filter_message(blocked, session.filters, extracted.needs_dates),
                )

        filters = session.filters
        if extracted.intent in {"details", "availability", "pricing"}:
            return await self._vehicle_follow_up(session, extracted.intent,
                                                 extracted.selected_index, token,
                                                 allow_stale=allow_selected_car_for_this_turn)
        if session.search_mode == "guided":
            question = next_question(
                session.search_mode, filters, anywhere=session.anywhere,
                budget_decided=session.budget_decided,
            )
            if question == "location":
                session.pending_requirement = "location"
                return self._response(session, "clarification", location_prompt(filters))
            if question == "dates":
                session.pending_requirement = "dates"
                return self._response(
                    session, "clarification", dates_prompt(
                        pickup_only=filters.from_date is None and (
                            filters.rental_days is not None or filters.to_date is not None
                        ),
                        return_only=filters.from_date is not None and filters.to_date is None,
                        today=self.today(),
                    ),
                )
            if question == "budget":
                session.pending_budget = True
                session.pending_requirement = None
                return self._response(
                    session, "clarification",
                    "Do you have a daily budget? You can also say 'Show options without a limit'.",
                    suggestions=["Show options without a limit"],
                )
            if filters.max_price is not None and filters.budget_period is None:
                session.pending_requirement = "budget_period"
                return self._response(
                    session, "clarification",
                    f"Is your AED {filters.max_price:g} budget per day or for the whole trip?",
                    suggestions=["Per day", "Whole trip"],
                )
        needs_dates = (
            extracted.needs_dates or (filters.rental_days is not None and filters.from_date is None)
        ) and not (filters.from_date and filters.to_date)
        if extracted.needs_location and request.context.lat is None:
            session.pending_requirement = ("location_then_dates" if needs_dates
                                           else "location")
            prompt = near_me_prompt() if extracted.sort_by == "distance" else location_prompt()
            return self._response(session, "clarification", prompt)
        if session.pending_requirement in {"location", "location_then_dates"}:
            if not (filters.location or filters.lat is not None):
                return self._response(session, "clarification", location_prompt())
            session.pending_requirement = ("dates" if session.pending_requirement == "location_then_dates"
                                           or needs_dates else None)
        if needs_dates or session.pending_requirement == "dates":
            if filters.from_date and filters.to_date:
                session.pending_requirement = None
            else:
                session.pending_requirement = "dates"
                prompt = dates_prompt(
                    pickup_only=(filters.from_date is None
                                 and (filters.to_date is not None
                                      or filters.rental_days is not None)),
                    return_only=filters.from_date is not None and filters.to_date is None,
                )
                if budget_ack:
                    prompt = f"{budget_ack} {prompt}"
                return self._response(
                    session, "clarification",
                    prompt,
                )
        if filters.sort_by == "distance" and filters.lat is None:
            return self._response(session, "clarification", near_me_prompt())
        if (filters.from_date is None) != (filters.to_date is None):
            return self._response(
                session, "clarification",
                dates_prompt(
                    pickup_only=filters.from_date is None,
                    return_only=filters.to_date is None,
                ),
            )
        if (session.search_mode == "guided"
                and not (filters.location or filters.lat is not None or session.anywhere)):
            session.pending_requirement = "location"
            return self._response(session, "clarification", location_prompt(filters))
        catalog_error = await self._resolve_catalog_filters(
            session, request.message, token,
        )
        if catalog_error:
            return catalog_error
        filters = session.filters
        if not filters.query_params():
            if any((filters.family_suitable, filters.seats, filters.fuel_type,
                    filters.fuel_efficiency_preferred, filters.transmission)):
                session.pending_requirement = "location"
                return self._response(session, "clarification", location_prompt())
            return self._response(session, "clarification", empty_search_prompt())
        return await self._search_vehicles(session, token, request_message=request.message,
                                           budget_ack=budget_ack,
                                           catalog_skipped=catalog_skipped)

    async def _handle_local_turn(
        self, session: Session, request: ChatRequest, token: str | None,
    ) -> ChatResponse | None:
        message = request.message
        if field := context_question(message):
            return self._context_answer(session, field)
        if cancels_pending_change(message):
            session.pending_budget = False
            if session.pending_requirement in {
                "location", "dates", "vehicle_type", "budget_period",
                "availability_dates", "availability_times",
            }:
                session.pending_requirement = None
            if session.search_mode == "guided" and session.filters.max_price is None:
                session.budget_decided = True
            return self._response(session, "guidance",
                                  "Okay, I've kept your existing search details.")
        if starts_guided_search(message):
            session.search_mode = "guided"
            if normalized(message) == "family trip":
                session.filters = session.filters.model_copy(update={
                    "family_suitable": True,
                })
                session.criteria_revision += 1
                return await self._continue_search(session, token)
            if normalized(message) == "budget cars":
                session.filters = session.filters.model_copy(update={
                    "sort_by": "price_low",
                })
                session.criteria_revision += 1
                return await self._continue_search(session, token)
            if normalized(message) == "cars near me":
                session.pending_requirement = "location"
                return self._response(session, "clarification", near_me_prompt())
            if normalized(message) == "find a car":
                return await self._continue_search(session, token)
        if starts_browsing(message):
            session.search_mode = "browse"
            session.pending_budget = False
            return await self._continue_search(session, token)
        if session.pending_requirement == "budget_period":
            answer = normalized(message)
            if answer in {"per day", "daily", "per day budget", "daily budget"}:
                session.filters = session.filters.model_copy(update={"budget_period": "day"})
                session.pending_requirement = None
                session.criteria_revision += 1
                return await self._continue_search(session, token)
            if answer in {"whole trip", "for the whole trip", "total", "total budget",
                          "for the trip"}:
                session.trip_budget = session.filters.max_price
                session.filters = session.filters.model_copy(update={
                    "max_price": None, "min_price": None, "budget_period": "trip",
                })
                session.pending_requirement = None
                session.criteria_revision += 1
                return await self._continue_search(session, token)
        if explicit_anywhere(message):
            session.anywhere = True
            session.location_source = None
            session.filters = session.filters.model_copy(update={
                "location": None, "lat": None, "lng": None,
            })
            session.pending_requirement = None
            session.criteria_revision += 1
            return await self._continue_search(session, token)
        if clear_field := clear_request(message):
            if clear_field == "budget":
                session.filters = session.filters.model_copy(update={
                    "min_price": None, "max_price": None, "budget_period": None,
                })
                session.trip_budget = None
                session.budget_decided = True
                session.pending_budget = False
            else:
                session.filters = session.filters.model_copy(update={
                    "car_type_id": None, "car_type_ids": None,
                    "car_type_name": None, "search": None,
                })
                session.pending_requirement = None
            session.criteria_revision += 1
            return await self._continue_search(session, token)
        if (new_area := corrected_location(message)) and (
            session.filters.location is not None
            or session.pending_requirement in {"location", "location_then_dates"}
        ):
            session.filters = session.filters.model_copy(update={"location": new_area})
            session.anywhere = False
            session.location_source = "typed"
            session.field_provenance["location"] = "typed"
            session.criteria_revision += 1
            return await self._continue_search(session, token)
        if action := change_request(message):
            if action == "budget":
                session.pending_budget = True
                return self._response(session, "clarification", budget_amount_prompt())
            if action == "dates":
                session.pending_requirement = "dates"
                return self._response(
                    session, "clarification", dates_prompt(today=self.today()),
                )
            if action == "location":
                session.pending_requirement = "location"
                return self._response(
                    session, "clarification", "Which city or area should I try?",
                )
            session.pending_requirement = "vehicle_type"
            return self._response(
                session, "clarification",
                "What vehicle type would you like? You can also say 'Any vehicle type'.",
                suggestions=["Any vehicle type"],
            )
        if skips_budget(message) and (session.pending_budget or session.search_mode == "guided"):
            if session.pending_requirement == "budget_period":
                session.filters = session.filters.model_copy(update={
                    "min_price": None, "max_price": None, "budget_period": None,
                })
                session.pending_requirement = None
                session.trip_budget = None
            session.budget_decided = True
            session.pending_budget = False
            return await self._continue_search(session, token)
        if normalized(message) in {"retry", "try again"} and session.last_search_outcome == "unavailable":
            return await self._continue_search(session, token)
        if normalized(message) in {"cheapest first", "recommended order"}:
            session.filters = session.filters.model_copy(update={
                "sort_by": ("price_low" if normalized(message) == "cheapest first"
                            else "recommended"),
            })
            session.criteria_revision += 1
            return await self._continue_search(session, token)
        if normalized(message) in {"how booking works", "booking help"}:
            return self._response(
                session, "guidance", booking_guidance_message(),
                suggestions=booking_guidance_suggestions(),
            )
        if normalized(message) == "change requirements":
            return self._response(
                session, "guidance",
                "Which detail should I change: area, dates, budget, or vehicle type?",
                suggestions=["Change area", "Change dates", "Change budget",
                             "Different vehicle type"],
            )
        if normalized(message) == "sign in":
            if session.pending_operation is not None:
                if token:
                    return await self._resume_pending_operation(session, token)
                return self._authentication_required(
                    session, "Please sign in to continue your saved request.",
                )
            return self._response(session, "guidance", booking_guidance_message())
        if normalized(message) == "check availability":
            return await self._vehicle_follow_up(session, "availability", None, token)
        option_availability = re.fullmatch(
            r"check availability for option\s+(\d+)", normalized(message),
        )
        if option_availability:
            return await self._vehicle_follow_up(
                session, "availability", int(option_availability.group(1)), token,
            )
        if normalized(message) == "price details":
            card = self._resolve_focus_card(session, None)
            if card is None:
                return self._response(
                    session, "clarification", "Which car's price should I check?",
                )
            if card.price is None:
                return self._response(
                    session, "unavailable", "I don't have a verified daily price for this car.",
                )
            return self._response(
                session, "guidance",
                f"This car is listed at AED {card.price:g} per day. "
                "Review the final price in the Gear app before booking.",
                vehicles=[card],
            )
        return None

    async def _continue_search(self, session: Session, token: str | None) -> ChatResponse:
        question = next_question(
            session.search_mode, session.filters, anywhere=session.anywhere,
            budget_decided=session.budget_decided,
        )
        if question == "location":
            session.pending_requirement = "location"
            return self._response(
                session, "clarification", location_prompt(session.filters),
            )
        if question == "dates":
            session.pending_requirement = "dates"
            return self._response(
                session, "clarification", dates_prompt(
                    pickup_only=session.filters.from_date is None and (
                        session.filters.rental_days is not None
                        or session.filters.to_date is not None
                    ),
                    return_only=(session.filters.from_date is not None
                                 and session.filters.to_date is None),
                    today=self.today(),
                ),
            )
        if question == "budget":
            session.pending_budget = True
            session.pending_requirement = None
            return self._response(
                session, "clarification",
                "Do you have a daily budget? You can also say 'Show options without a limit'.",
                suggestions=["Show options without a limit"],
            )
        if (session.search_mode == "guided" and session.filters.max_price is not None
                and session.filters.budget_period is None):
            session.pending_requirement = "budget_period"
            return self._response(
                session, "clarification",
                f"Is your AED {session.filters.max_price:g} budget per day or for the whole trip?",
                suggestions=["Per day", "Whole trip"],
            )
        if date_error := self._past_pickup_error(session):
            return date_error
        session.pending_requirement = None
        if catalog_error := await self._resolve_catalog_filters(session, "", token):
            return catalog_error
        return await self._search_vehicles(session, token)

    def _context_answer(self, session: Session, field: str) -> ChatResponse:
        filters = session.filters
        if field == "location":
            if filters.location:
                lead = ("You said" if session.location_source == "typed" else
                        "The current search area is")
                reply = f"{lead} {filters.location}."
            elif filters.lat is not None:
                reply = "I'm using the location you shared; you haven't named an area."
            elif session.anywhere:
                reply = "You chose to search anywhere; you haven't named an area."
            else:
                reply = "You haven't specified an area yet."
                if known_dates := dates_description(filters):
                    reply += f" I have your dates as {known_dates}."
                reply += " Which city or area would you like?"
        elif field == "dates":
            dates = dates_description(filters)
            reply = (f"Your rental dates are {dates}." if dates else
                     "You haven't provided rental dates yet.")
        elif field == "budget":
            if session.trip_budget is not None:
                reply = f"You mentioned AED {session.trip_budget:g} for the whole trip. I haven't applied that as a daily price limit."
            elif filters.max_price is not None:
                period = (" per day" if filters.budget_period == "day" else
                          " (you haven't specified whether that is per day or total)")
                reply = f"Your current maximum budget is AED {filters.max_price:g}{period}."
            elif session.budget_decided:
                reply = "You chose to search without a budget limit."
            else:
                reply = "You haven't provided a budget yet."
        elif field == "vehicle_type":
            requested_type = filters.car_type_name or filters.search
            reply = (f"Your current vehicle preference is {requested_type}." if requested_type
                     else "You haven't specified a vehicle type.")
        elif field == "reason":
            if session.pending_budget:
                reply = "I'm asking about your budget so I can narrow the cars by a price you choose. You can skip it."
            elif session.pending_requirement in {"location", "location_then_dates"}:
                reply = "I'm asking for an area so I can search the right place."
            elif session.pending_requirement in {"dates", "availability_dates"}:
                reply = "I'm asking for dates so I can check cars for your rental period."
            elif session.last_search_outcome == "unavailable":
                reply = "The search service couldn't complete the request. Your search details are saved; you can retry."
            else:
                reply = "I use the details you've given to find matching cars."
        else:
            summary = criteria_summary(filters, lead="Your current request")
            reply = summary if filters.query_params() else "You haven't given me search details yet."
            if session.last_search_outcome == "empty":
                reply += " The last completed search found no verified matches."
        return self._response(session, "guidance", reply)

    @staticmethod
    def _extraction_context(session: Session) -> dict[str, object]:
        return {
            "task": session.search_mode,
            "pending_question": "budget" if session.pending_budget else session.pending_requirement,
            "location_source": session.location_source,
            "anywhere": session.anywhere,
            "budget_decided": session.budget_decided,
            "trip_budget": session.trip_budget,
            "last_assistant_action": session.last_assistant_action,
            "last_search_outcome": session.last_search_outcome,
            "recent_user_turns": session.recent_turns[-4:],
        }

    def _fallback_extraction(self, request: ChatRequest, session: Session) -> Extraction | None:
        """Handle clear date and budget clarifications without waiting for the model."""
        date_question_pending = session.pending_requirement in {
            "dates", "location", "location_then_dates",
        }
        try:
            has_named_date_range = (
                date_question_pending
                and resolve_month_day_range(request.message, self.today()) is not None
            )
        except ValueError:
            # Let _merge_extracted_filters return its normal invalid-date clarification.
            has_named_date_range = date_question_pending
        pending_bare_date = (
            has_bare_day_range(request.message)
            and date_question_pending
        )
        plain_location = (
            session.pending_requirement in {"location", "location_then_dates"}
            and re.fullmatch(r"[A-Za-z][A-Za-z '\-]{1,80}", request.message) is not None
            and not has_named_date_range
            and not pending_bare_date
        )
        if not (
            has_named_date_range
            or pending_bare_date
            or plain_location
            or session.pending_budget
            or budget_change_requested(request.message)
        ):
            return None
        # This local parser cannot safely extract extra specifications or pickup times.
        # Let the contextual extractor handle rich turns instead of silently discarding them.
        if re.search(
            r"\b(?:\d{1,2}:\d{2}|\d{1,2}\s*(?:am|pm)|suv|sedan|van|"
            r"automatic|manual|hybrid|electric|seats?|luggage|pickup time|return time)\b",
            request.message, re.IGNORECASE,
        ):
            return None
        location = next((name for name in (
            "Dubai Marina", "Abu Dhabi", "Sharjah", "Ajman", "Dubai",
        ) if name.casefold() in request.message.casefold()), None)
        if plain_location and location is None:
            location = request.message.strip()
        max_price = budget_amount(request.message)
        if max_price is None and session.pending_budget:
            max_price = budget_amount(request.message, allow_bare=True)
        budget_period = None
        if max_price is not None:
            if re.search(r"\b(?:whole trip|total|trip budget)\b", request.message,
                         re.IGNORECASE):
                budget_period = "trip"
            elif (session.pending_budget or
                  re.search(r"\b(?:per\s*day|daily|/\s*day)\b", request.message,
                            re.IGNORECASE)):
                budget_period = "day"
        return Extraction(
            intent="search",
            selected_index=None,
            reset_filters=False,
            needs_dates=False,
            needs_location=False,
            unsupported_constraints=[],
            location=location,
            search=None,
            from_date=None,
            to_date=None,
            from_time=None,
            to_time=None,
            min_price=None,
            max_price=max_price,
            budget_period=budget_period,
        )

    async def _resolve_catalog_filters(
        self, session: Session, request_message: str, token: str | None,
    ) -> ChatResponse | None:
        filters = session.filters
        unresolved_type = filters.car_type_name if not filters.car_type_id else None
        unresolved_brand = filters.brand_name if not filters.brand_id else None
        if self.data_source == "demo" or not (unresolved_type or unresolved_brand):
            return None
        if not token:
            session.pending_operation = PendingProtectedOperation(
                "catalog_search", request_message=request_message,
            )
            return self._authentication_required(
                session,
                "Please sign in so I can match that vehicle type or brand to Gear's verified catalog.",
            )
        try:
            catalog = await self.gear.filter_catalog(token)
        except GearAuthorizationRequired:
            session.pending_operation = PendingProtectedOperation(
                "catalog_search", request_message=request_message,
            )
            return self._authentication_required(
                session, "Please sign in again so I can use Gear's vehicle catalog.",
            )
        except (GearUnavailable, ConfigurationError):
            return self._response(
                session, "unavailable",
                "I can't load Gear's vehicle types right now. Please try again shortly.",
            )

        values = filters.model_dump(exclude_none=True)
        if unresolved_type:
            type_id = resolve_catalog_id(catalog, "car_type", unresolved_type)
            if type_id is None:
                return self._response(
                    session, "clarification",
                    f"I couldn't find {unresolved_type} in Gear's current vehicle-type catalog. "
                    "Which vehicle type should I use?",
                )
            values["car_type_id"] = type_id
        if unresolved_brand:
            brand_id = resolve_catalog_id(catalog, "brand", unresolved_brand)
            if brand_id is None:
                return self._response(
                    session, "clarification",
                    f"I couldn't find {unresolved_brand} in Gear's current brand catalog. "
                    "Which brand should I use?",
                )
            values["brand_id"] = brand_id
        session.filters = SearchFilters.model_validate(values)
        session.pending_operation = None
        return None

    async def _search_vehicles(self, session: Session, token: str | None,
                               *, request_message: str = "", budget_ack: str | None = None,
                               catalog_skipped: bool = False) -> ChatResponse:
        filters = session.filters
        searches_availability = (filters.from_date is not None
                                 and filters.to_date is not None
                                 and self.data_source == "gear")
        if searches_availability and not token:
            session.pending_operation = PendingProtectedOperation(
                "search_availability", request_message=request_message,
                catalog_skipped=catalog_skipped)
            return self._authentication_required(
                session, "Please sign in to search vehicles available for those dates.")
        had_prior_results = bool(session.results)
        try:
            if searches_availability:
                cars = await self.gear.search_available_cars(filters, token)
            else:
                cars = await self.gear.search_cars(filters, None)
        except GearAuthorizationRequired:
            session.pending_operation = PendingProtectedOperation(
                "search_availability", request_message=request_message,
                catalog_skipped=catalog_skipped)
            return self._authentication_required(
                session, "Please sign in again to search available vehicles.")
        except (GearUnavailable, ConfigurationError):
            session.pending_operation = None
            session.last_search_outcome = "unavailable"
            return self._response(session, "unavailable",
                                  "I can't search right now. Your search details are saved; "
                                  "please try again.",
                                  suggestions=["Retry"])
        cars, missing_attribute = self._apply_verified_preferences(
            cars, filters, apply_unforwarded=searches_availability,
        )
        if missing_attribute:
            session.pending_operation = None
            return self._response(
                session, "unavailable",
                f"Gear's current listings don't provide verified {missing_attribute} data, "
                "so I can't apply that requirement safely yet.",
                suggestions=["Change requirements"],
            )
        session.results = cars
        session.last_search_filters = filters.model_copy(deep=True)
        session.last_search_outcome = "results" if cars else "empty"
        session.results_revision = session.criteria_revision
        session.pending_operation = None
        session.selected_vehicle_id = None
        session.pending_vehicle_intent = None
        if missing_verified_rating(request_message, cars):
            return self._response(session, "guidance",
                                  "I don't have verified rating data for these results.")
        if not cars:
            if budget_ack:
                date_scope = dates_description(filters)
                scope = f" for {date_scope}" if date_scope else ""
                message = (
                    f"{budget_ack} I searched{scope} but couldn't find a verified match "
                    "for those filters right now. I can broaden the search if you'd like."
                )
            else:
                message = no_results_message(filters)
            if session.trip_budget is not None:
                message += (f" Your AED {session.trip_budget:g} trip budget was not used "
                            "as a daily price limit.")
            return self._response(session, "results", message,
                                  suggestions=no_results_suggestions(
                                      filters, anywhere=session.anywhere,
                                      trip_budget=session.trip_budget is not None,
                                  ))
        cars = with_match_reasons(cars, filters)
        refinement = None
        if budget_ack:
            refinement = budget_ack.rstrip(".")
        elif had_prior_results:
            refinement = criteria_summary(filters, lead="Sure").rstrip(".")
        message = results_message(len(cars), filters, catalog_skipped=catalog_skipped,
                                  refinement_note=refinement)
        if session.trip_budget is not None:
            message += (f" Your AED {session.trip_budget:g} trip budget was not used "
                        "as a daily price limit; review the final total in the Gear app.")
        return self._response(
            session, "results", message,
            vehicles=cars,
            suggestions=results_suggestions(filters, has_single_result=len(cars) == 1),
        )

    @staticmethod
    def _apply_verified_preferences(
        cars: list[VehicleCard], filters: SearchFilters, *, apply_unforwarded: bool = False,
    ) -> tuple[list[VehicleCard], str | None]:
        filtered = list(cars)
        required_fields = (
            (filters.seats is not None, "seating capacity", lambda car: car.seats is not None),
            (filters.fuel_type is not None, "fuel type", lambda car: car.fuel_type is not None),
            (filters.transmission is not None, "transmission", lambda car: car.transmission is not None),
            (
                filters.fuel_efficiency_preferred is True,
                "fuel efficiency",
                lambda car: car.fuel_efficiency is not None or car.is_fuel_efficient is not None,
            ),
        )
        for requested, label, has_value in required_fields:
            if requested and filtered and not any(has_value(car) for car in filtered):
                return [], label
        if apply_unforwarded and filters.location:
            if filtered and not any(car.location for car in filtered):
                return [], "location"
            wanted_location = filters.location.casefold()
            filtered = [car for car in filtered
                        if car.location and wanted_location in car.location.casefold()]
        if apply_unforwarded and filters.search:
            wanted_search = filters.search.casefold()
            filtered = [car for car in filtered
                        if car.title and wanted_search in car.title.casefold()]
        if apply_unforwarded and filters.car_type_ids:
            allowed_types = {value.casefold() for value in filters.car_type_ids}
            if filtered and not any(car.car_type_id for car in filtered):
                return [], "vehicle type"
            filtered = [car for car in filtered
                        if car.car_type_id and car.car_type_id.casefold() in allowed_types]
        if apply_unforwarded and filters.min_rating is not None:
            if filtered and not any(car.rating is not None for car in filtered):
                return [], "rating"
            filtered = [car for car in filtered
                        if car.rating is not None and car.rating >= filters.min_rating]
        if apply_unforwarded and filters.featured is not None:
            if filtered and not any(car.is_featured is not None for car in filtered):
                return [], "featured-listing"
            filtered = [car for car in filtered if car.is_featured is filters.featured]
        if filters.seats is not None:
            filtered = [car for car in filtered
                        if car.seats is not None and car.seats >= filters.seats]
        if filters.fuel_type:
            wanted = filters.fuel_type.casefold()
            filtered = [car for car in filtered
                        if car.fuel_type and car.fuel_type.casefold() == wanted]
        if filters.transmission:
            wanted = filters.transmission.casefold()
            filtered = [car for car in filtered
                        if car.transmission and car.transmission.casefold() == wanted]
        if filters.budget_period == "trip" and filters.max_price is not None:
            if not filters.rental_days:
                return [], "rental duration for a total-trip budget"
            if filtered and not any(car.price is not None for car in filtered):
                return [], "daily price"
            filtered = [
                car for car in filtered
                if car.price is not None and car.price * filters.rental_days <= filters.max_price
            ]
        if filters.fuel_efficiency_preferred:
            filtered.sort(
                key=lambda car: (
                    car.is_fuel_efficient is True,
                    car.fuel_efficiency if car.fuel_efficiency is not None else -1,
                ),
                reverse=True,
            )
        if filters.family_suitable:
            def family_score(car: VehicleCard) -> int:
                return (
                    (100 if car.is_family_suitable is True else 0)
                    + (30 if car.seats is not None and car.seats >= 5 else 0)
                    + (20 if car.luggage_capacity is not None and car.luggage_capacity >= 2 else 0)
                    + (10 if (car.car_type_name or "").casefold()
                       in {"suv", "van", "minivan", "mpv"} else 0)
                )

            if any(family_score(car) for car in filtered):
                filtered.sort(key=family_score, reverse=True)
        if filters.sort_by in {"price_low", "price_high"}:
            filtered.sort(
                key=lambda car: car.price if car.price is not None else float("inf"),
                reverse=filters.sort_by == "price_high",
            )
        elif filters.sort_by == "rating":
            filtered.sort(key=lambda car: car.rating if car.rating is not None else -1,
                          reverse=True)
        return filtered, None

    async def _vehicle_spec_answer(self, session: Session, topic: SpecTopic,
                                 card: VehicleCard, token: str | None) -> ChatResponse:
        session.selected_vehicle_id = card.id
        enriched = card
        if self.data_source == "gear":
            if token:
                try:
                    enriched = await self.gear.car_details(card.id, token)
                except GearAuthorizationRequired:
                    session.pending_operation = PendingProtectedOperation(
                        "details", card.id, criteria_revision=session.criteria_revision,
                    )
                    return self._authentication_required(
                        session,
                        "Sign in and I can pull this car's verified specifications.",
                        vehicles=[card],
                    )
                except (GearUnavailable, ConfigurationError):
                    enriched = card
            elif self._spec_data_missing(card, topic):
                session.pending_operation = PendingProtectedOperation(
                    "details", card.id, criteria_revision=session.criteria_revision,
                )
                return self._authentication_required(
                    session,
                    "Sign in and I can pull this car's verified specifications.",
                    vehicles=[card],
                )
        message = vehicle_spec_message(enriched, topic)
        suggestions = ["Check availability", "Different vehicle type", "How booking works"]
        return self._response(session, "details", message, vehicles=[enriched],
                              suggestions=suggestions)

    @staticmethod
    def _spec_data_missing(card: VehicleCard, topic: SpecTopic) -> bool:
        if topic == "seats":
            return card.seats is None
        if topic == "mileage":
            return card.fuel_efficiency is None and card.is_fuel_efficient is None
        if topic == "fuel":
            return card.fuel_type is None
        if topic == "transmission":
            return card.transmission is None
        if topic == "luggage":
            return card.luggage_capacity is None
        return not any((
            card.seats is not None, card.fuel_type, card.transmission,
            card.fuel_efficiency is not None, card.is_fuel_efficient is not None,
            card.luggage_capacity is not None,
        ))

    def _resolve_focus_card(self, session: Session,
                            selected_index: int | None) -> VehicleCard | None:
        card = self._selected_card(session, selected_index)
        if card is not None:
            return card
        return self._card_by_id(session, session.selected_vehicle_id)

    async def _vehicle_follow_up(self, session: Session, intent: str,
                                 selected_index: int | None,
                                 token: str | None,
                                 *, allow_stale: bool = False) -> ChatResponse:
        card = self._selected_card(session, selected_index)
        if card is None and allow_stale and isinstance(selected_index, int) and not isinstance(selected_index, bool):
            if 1 <= selected_index <= len(session.results):
                card = session.results[selected_index - 1]
        if card is None and selected_index is None:
            selected_id = session.selected_vehicle_id
            if selected_id is None and session.pending_operation is not None:
                selected_id = session.pending_operation.vehicle_id
            card = self._card_by_id(session, selected_id)
            if card is None and allow_stale and selected_id is not None:
                card = next((item for item in session.results if item.id == selected_id), None)
        if card is None:
            session.pending_operation = None
            session.pending_vehicle_intent = None
            return self._response(session, "clarification",
                                  "Which car do you mean? Search first if you don't see options yet.")
        session.selected_vehicle_id = card.id
        if intent == "pricing":
            message = ("Checkout totals aren't available in this demo."
                       if self.data_source == "demo" else
                       "I can't verify the checkout total yet — please review the price in the app.")
            return self._response(session, "unavailable",
                                  message, vehicles=[card])
        if intent == "availability":
            session.pending_operation = None
            session.pending_vehicle_intent = "availability"
            filters = session.filters
            if not (filters.from_date and filters.to_date):
                session.pending_requirement = "availability_dates"
                return self._response(
                    session, "clarification",
                    dates_prompt(
                        pickup_only=filters.from_date is None and filters.to_date is not None,
                        return_only=filters.from_date is not None and filters.to_date is None,
                    ),
                )
            if not (filters.from_time and filters.to_time):
                session.pending_requirement = "availability_times"
                return self._response(session, "clarification",
                                      "What pickup and return times should I check for those dates?")
            session.pending_requirement = None
            session.pending_vehicle_intent = None
        pending = PendingProtectedOperation(
            "details" if intent == "details" else "vehicle_availability", card.id,
            criteria_revision=session.criteria_revision,
        )
        if self.data_source == "gear" and not token:
            session.pending_operation = pending
            return self._authentication_required(
                session,
                ("Please sign in to view this car's full details."
                 if intent == "details" else
                 "Please sign in to check this car's live availability."),
                vehicles=[card],
            )
        try:
            if intent == "details":
                details = await self.gear.car_details(card.id, token)
                session.pending_operation = None
                session.pending_vehicle_intent = None
                details = with_match_reasons([details], session.filters)[0]
                message = "Here's what I found for this car."
                index = session.results.index(card) + 1
                return self._response(session, "details", message,
                                    vehicles=[details],
                                    suggestions=[f"Check availability for option {index}"])
            available = await self.gear.check_availability(card.id, session.filters, token)
            session.pending_operation = None
            if self.data_source == "demo":
                message = ("In this demo, the sample vehicle is available for those dates and times."
                           if available else
                           "In this demo, the sample vehicle is unavailable for those dates and times.")
            elif available:
                message = (
                    "This car is available for your selected dates. To book it, open the "
                    "Gear app, select this car, and complete the booking there."
                )
            else:
                message = unavailable_vehicle_message()
            suggestions = (["How booking works", "Different vehicle type"] if available
                           else unavailable_vehicle_suggestions())
            return self._response(session, "availability", message, vehicles=[card],
                                  available=available, suggestions=suggestions)
        except GearAuthorizationRequired:
            session.pending_operation = pending
            return self._authentication_required(
                session, "Please sign in again to view protected Gear information.",
                vehicles=[card],
            )
        except (GearUnavailable, ConfigurationError):
            session.pending_operation = None
            return self._response(session, "unavailable",
                                  "I can't verify this car right now. Please try again shortly.")

    async def _resume_pending_operation(self, session: Session,
                                        token: str) -> ChatResponse:
        pending = session.pending_operation
        if pending is None:
            raise RuntimeError("No pending operation to resume")
        if pending.kind == "catalog_search":
            session.pending_operation = None
            return await self._chat_turn(
                ChatRequest(message=pending.request_message, session_id=session.public_token),
                token,
                session,
            )
        if pending.kind == "search_availability":
            date_error = self._past_pickup_error(session)
            if date_error:
                session.pending_operation = None
                return date_error
            return await self._search_vehicles(session, token,
                                               request_message=pending.request_message,
                                               catalog_skipped=pending.catalog_skipped)
        if pending.kind == "vehicle_availability":
            date_error = self._past_pickup_error(session)
            if date_error:
                session.pending_operation = None
                return date_error
        index = next((position for position, card in enumerate(session.results, start=1)
                      if card.id == pending.vehicle_id), None)
        return await self._vehicle_follow_up(
            session, "details" if pending.kind == "details" else "availability", index,
            token, allow_stale=pending.criteria_revision == session.criteria_revision,
        )

    def _merge_extracted_filters(self, session: Session, request: ChatRequest,
                                 extracted: Extraction) -> ChatResponse | None:
        previous_filters = session.filters.model_copy(deep=True)
        updates = {key: value for key, value in extracted.filter_updates().items()
                   if value != ""}
        if "car_type" in extracted.unsupported_constraints and extracted.search:
            updates["car_type_name"] = extracted.search
            updates.pop("search", None)
        if "brand" in extracted.unsupported_constraints and extracted.search:
            updates["brand_name"] = extracted.search
            updates.pop("search", None)
        updates.update(verified_preference_updates(request.message))
        reset = extracted.reset_filters and explicit_search_reset(request.message)
        awaiting_budget = session.pending_budget and not reset
        budget_requested = budget_change_requested(request.message) and not reset
        needs_budget_amount = False
        has_price_update = "min_price" in updates or "max_price" in updates
        if not (awaiting_budget or budget_requested):
            explicit_amount = budget_amount(request.message)
            lower_bound = re.search(
                r"\b(?:over|above|more\s+than|min(?:imum)?)\s*(?:aed\s*)?\d",
                request.message, re.IGNORECASE,
            ) is not None
            if explicit_amount is not None and not lower_bound:
                updates["max_price"] = explicit_amount
                has_price_update = True
        if ("budget_period" in updates and "max_price" not in updates
                and session.filters.max_price is None):
            # Asking about a three-day checkout total is not a trip budget.
            updates.pop("budget_period")
        if awaiting_budget or budget_requested:
            is_relative_increase = re.search(
                r"\b(?:increase|raise|expand)\b.{0,35}\bby\s+(?:aed\s*)?\d",
                request.message, re.IGNORECASE,
            ) is not None
            amount = (None if is_relative_increase else
                      budget_amount(request.message, allow_bare=awaiting_budget))
            if amount is None:
                updates.pop("min_price", None)
                updates.pop("max_price", None)
                has_price_update = False
                needs_budget_amount = True
            else:
                updates["max_price"] = amount
                has_price_update = True
                if "budget_period" not in updates and awaiting_budget:
                    updates["budget_period"] = "day"
        if reset:
            session.pending_requirement = None
            session.pending_budget = False
            session.search_mode = ("guided" if starts_guided_search(request.message)
                                   else "browse")
            session.anywhere = False
            session.budget_decided = False
            session.trip_budget = None
            session.location_source = None
            session.field_provenance.clear()
            session.pending_operation = None
            session.selected_vehicle_id = None
            session.pending_vehicle_intent = None
            session.results = []
        try:
            relative_dates = resolve_month_day_range(request.message, self.today())
        except ValueError:
            return self._response(session, "clarification",
                                  "Those days aren't a valid pickup and return range for that month. "
                                  "Which days should I use?")
        extracted_dates_are_iso = False
        if extracted.from_date and extracted.to_date:
            try:
                date.fromisoformat(extracted.from_date)
                date.fromisoformat(extracted.to_date)
                extracted_dates_are_iso = True
            except ValueError:
                pass
        ambiguous_day_range = (
            relative_dates is None
            and has_bare_day_range(request.message)
            and not extracted_dates_are_iso
            and bool(extracted.from_date or extracted.to_date
                     or session.pending_vehicle_intent == "availability"
                     or session.pending_requirement in {"dates", "availability_dates",
                                                        "location", "location_then_dates"})
        )
        if ambiguous_day_range:
            updates.pop("from_date", None)
            updates.pop("to_date", None)
        existing = {} if reset else session.filters.model_dump(exclude_none=True)
        if "car_type_name" in updates:
            existing.pop("car_type_id", None)
            existing.pop("car_type_ids", None)
        if "brand_name" in updates:
            existing.pop("brand_id", None)
        existing.update(updates)
        if has_price_update and "budget_period" not in updates:
            existing.pop("budget_period", None)
        trip_budget = None
        if updates.get("budget_period") == "trip" and "max_price" in updates:
            trip_budget = float(updates["max_price"])
            existing.pop("max_price", None)
            existing.pop("min_price", None)
        if relative_dates:
            existing["from_date"] = relative_dates[0]
            if relative_dates[1] is not None:
                existing["to_date"] = relative_dates[1]
        duration_changed = "rental_days" in updates
        pickup_changed = "from_date" in updates or bool(relative_dates)
        return_changed = "to_date" in updates or bool(relative_dates and relative_dates[1])
        if (existing.get("rental_days") and existing.get("from_date")
                and (duration_changed or pickup_changed) and not return_changed):
            pickup = existing["from_date"]
            if isinstance(pickup, str):
                try:
                    pickup = date.fromisoformat(pickup)
                except ValueError:
                    pickup = None
            if isinstance(pickup, date):
                existing["to_date"] = pickup + timedelta(days=int(existing["rental_days"]))
        if existing.get("from_date") and existing.get("to_date"):
            try:
                pickup = (date.fromisoformat(existing["from_date"])
                          if isinstance(existing["from_date"], str) else existing["from_date"])
                dropoff = (date.fromisoformat(existing["to_date"])
                           if isinstance(existing["to_date"], str) else existing["to_date"])
                existing["rental_days"] = max(1, (dropoff - pickup).days)
            except (TypeError, ValueError):
                pass
        if request.context.lat is not None:
            existing["lat"] = request.context.lat
            existing["lng"] = request.context.lng
        if request.context.car_type_id is not None:
            existing["car_type_id"] = request.context.car_type_id
            existing.pop("car_type_ids", None)
        elif request.context.car_type_ids is not None:
            existing["car_type_ids"] = request.context.car_type_ids
            existing.pop("car_type_id", None)
        if request.context.brand_id is not None:
            existing["brand_id"] = request.context.brand_id
        try:
            session.filters = SearchFilters.model_validate(existing)
        except ValidationError:
            return self._response(session, "clarification",
                                  "Please give valid dates and a valid price range.")
        if has_price_update and awaiting_budget:
            session.pending_budget = False
        if has_price_update:
            session.budget_decided = True
            session.trip_budget = trip_budget
        if session.filters.location and session.filters.location != previous_filters.location:
            session.location_source = "typed"
            session.anywhere = False
            session.field_provenance["location"] = "typed"
        if request.context.lat is not None:
            session.location_source = "shared"
            session.anywhere = False
            session.field_provenance["location"] = "shared"
        for field_name in updates:
            if field_name not in {"location", "lat", "lng"}:
                session.field_provenance[field_name] = "typed"
        if session.filters != previous_filters:
            session.criteria_revision += 1
        if needs_budget_amount:
            session.pending_budget = True
            return self._response(session, "clarification", budget_amount_prompt())
        if ambiguous_day_range:
            session.pending_requirement = ("availability_dates"
                                           if session.pending_vehicle_intent == "availability"
                                           else "dates")
            return self._response(
                session, "clarification",
                "Which month and year should I use for those pickup and return days?",
            )
        return None

    def _past_pickup_error(self, session: Session) -> ChatResponse | None:
        pickup = session.filters.from_date
        if pickup is not None and pickup < self.today():
            return self._response(
                session, "clarification",
                "That pickup date is in the past. What future pickup date should I use?",
            )
        return None

    @staticmethod
    def _selected_card(session: Session, index: int | None) -> VehicleCard | None:
        if session.results_revision != session.criteria_revision:
            return None
        if index is None and len(session.results) == 1:
            index = 1
        if isinstance(index, bool) or not isinstance(index, int):
            return None
        if 1 <= index <= len(session.results):
            return session.results[index - 1]
        return None

    @staticmethod
    def _card_by_id(session: Session, car_id: str | None) -> VehicleCard | None:
        if car_id is None or session.results_revision != session.criteria_revision:
            return None
        return next((card for card in session.results if card.id == car_id), None)

    @staticmethod
    def _automatic_resume_message(message: str) -> bool:
        normalized = " ".join(message.lower().strip(" .!?").split())
        return normalized in {
            "continue", "continue please", "retry", "try again", "proceed",
            "i signed in", "signed in", "i have signed in", "done",
        }

    @staticmethod
    def _message_matches_pending_operation(
        message: str, pending: PendingProtectedOperation,
    ) -> bool:
        lower = message.lower()
        mentions_availability = "availab" in lower or "check option" in lower
        if pending.kind == "catalog_search":
            return True
        if pending.kind == "details":
            return not mentions_availability and ("detail" in lower or "view option" in lower)
        return mentions_availability

    def _response(self, session: Session, status: str, message: str,
                  vehicles: list[VehicleCard] | None = None,
                  suggestions: list[str] | None = None,
                  available: bool | None = None,
                  action: ChatAction | None = None) -> ChatResponse:
        session.last_assistant_action = (
            f"asked_{'budget' if session.pending_budget else session.pending_requirement}"
            if status == "clarification" else status
        )
        if session.recovered_after_expiry:
            message = ("I couldn't restore your previous conversation; its session may have "
                       "expired. " + message)
            session.recovered_after_expiry = False
        return ChatResponse(session_id=session.public_token, status=status, message=message,
                            vehicles=vehicles or [], suggestions=suggestions or [],
                            applied_filters=session.filters, available=available,
                            data_source=self.data_source, action=action)

    def _authentication_required(self, session: Session, message: str,
                                 vehicles: list[VehicleCard] | None = None) -> ChatResponse:
        return self._response(
            session, "authentication_required", message, vehicles=vehicles,
            suggestions=["Sign in", "Keep browsing"],
            action=ChatAction(type="SIGN_IN"),
        )
