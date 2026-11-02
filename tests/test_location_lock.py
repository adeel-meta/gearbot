"""Location is requested once, then remembered until the user changes it."""

import asyncio
from datetime import date

from app.schemas.chat import ChatRequest
from app.security.sessions import SessionStore
from app.services.conversation import ConversationService
from app.services.dialogue_policy import no_results_actions
from app.schemas.chat import SearchFilters
from tests.test_chat_flow import FakeExtractor, FakeGear, extraction


def test_location_is_not_requested_again_after_the_first_answer():
    gear = FakeGear()
    chat = ConversationService(FakeExtractor([
        extraction(needs_location=True),
        extraction(location="Dubai", needs_location=True),
        extraction(needs_location=True, car_type_name="SUV"),
    ]), gear, SessionStore(1800))

    async def scenario():
        first = await chat.chat(ChatRequest(message="Find a car"), None)
        assert first.status == "clarification"
        assert "area" in first.message.lower() or "where" in first.message.lower()
        second = await chat.chat(ChatRequest(
            message="Dubai", session_id=first.session_id,
        ), None)
        assert second.applied_filters.location == "Dubai"
        third = await chat.chat(ChatRequest(
            message="show SUVs", session_id=first.session_id,
        ), None)
        assert "which city" not in third.message.lower()
        assert "where should i search" not in third.message.lower()
        assert third.applied_filters.location == "Dubai"

    asyncio.run(scenario())


def test_unanswered_location_prompt_does_not_lock_the_session():
    chat = ConversationService(FakeExtractor([
        extraction(needs_location=True),
        extraction(),
        extraction(location="Dubai"),
    ]), FakeGear(), SessionStore(1800))

    async def scenario():
        first = await chat.chat(ChatRequest(message="Find a car"), None)
        assert first.status == "clarification"
        missed = await chat.chat(ChatRequest(
            message="show SUVs?", session_id=first.session_id,
        ), None)
        assert missed.status == "clarification"
        assert "area" in missed.message.lower() or "where" in missed.message.lower()
        assert chat.sessions.get_or_create(first.session_id).location_locked is False
        assert chat.sessions.get_or_create(first.session_id).filters.location is None
        supplied = await chat.chat(ChatRequest(
            message="Dubai", session_id=first.session_id,
        ), None)
        assert supplied.applied_filters.location == "Dubai"
        assert chat.sessions.get_or_create(first.session_id).location_locked is True

    asyncio.run(scenario())


def test_location_remains_unlocked_until_the_user_provides_an_area():
    chat = ConversationService(FakeExtractor([
        extraction(), extraction(location="Dubai"), extraction(needs_location=True),
    ]), FakeGear(), SessionStore(1800))

    async def scenario():
        first = await chat.chat(ChatRequest(message="Find a car"), None)
        unclear = await chat.chat(ChatRequest(
            message="I'm not sure", session_id=first.session_id,
        ), None)
        session = chat.sessions.get_or_create(first.session_id)
        assert unclear.status == "clarification"
        assert "area" in unclear.message.lower() or "where" in unclear.message.lower()
        assert session.location_locked is False

        area = await chat.chat(ChatRequest(
            message="Dubai", session_id=first.session_id,
        ), None)
        assert area.applied_filters.location == "Dubai"
        assert session.location_locked is True

        follow_up = await chat.chat(ChatRequest(
            message="Show more cars", session_id=first.session_id,
        ), None)
        assert "which city" not in follow_up.message.lower()
        assert follow_up.applied_filters.location == "Dubai"

    asyncio.run(scenario())


def test_change_area_asks_once_more():
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai"),
        extraction(needs_location=True),
    ]), FakeGear(), SessionStore(1800))

    async def scenario():
        first = await chat.chat(ChatRequest(message="Cars in Dubai"), None)
        changed = await chat.chat(ChatRequest(
            message="change area", session_id=first.session_id,
        ), None)
        assert changed.status == "clarification"
        assert "area" in changed.message.lower()

    asyncio.run(scenario())


def test_memory_question_repeats_the_pending_date_question():
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai"),
    ]), FakeGear(), SessionStore(1800))

    async def scenario():
        first = await chat.chat(ChatRequest(message="Cars in Dubai"), None)
        dates = await chat.chat(ChatRequest(
            message="change dates", session_id=first.session_id,
        ), None)
        assert "date" in dates.message.lower()
        remembered = await chat.chat(ChatRequest(
            message="wat area did i give", session_id=first.session_id,
        ), None)
        assert "dubai" in remembered.message.lower()
        assert "date" in remembered.message.lower()
        assert remembered.applied_filters.location == "Dubai"

    asyncio.run(scenario())


def test_location_reason_question_answers_why_and_preserves_pending_dates():
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai"),
    ]), FakeGear(), SessionStore(1800))

    async def scenario():
        first = await chat.chat(ChatRequest(message="Cars in Dubai"), None)
        pending = await chat.chat(ChatRequest(
            message="change dates", session_id=first.session_id,
        ), None)
        assert "date" in pending.message.lower()

        answer = await chat.chat(ChatRequest(
            message="yes why u need my location?", session_id=first.session_id,
        ), None)
        assert "location" in answer.message.lower()
        assert "date" in answer.message.lower()
        assert answer.applied_filters.location == "Dubai"
        assert chat.sessions.get_or_create(first.session_id).pending_requirement == "dates"

    asyncio.run(scenario())


def test_cancelation_typo_does_not_replace_a_pending_budget_reply():
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai"),
    ]), FakeGear(), SessionStore(1800))

    async def scenario():
        first = await chat.chat(ChatRequest(message="Find a car in Dubai"), None)
        budget = await chat.chat(ChatRequest(
            message="set budget", session_id=first.session_id,
        ), None)
        assert "budget" in budget.message.lower()
        cancelled = await chat.chat(ChatRequest(
            message="cancelation", session_id=first.session_id,
        ), None)
        assert "cancellation-policy" not in cancelled.message
        assert "kept" in cancelled.message.lower()

    asyncio.run(scenario())


def test_place_typo_and_natural_location_change_keep_dates_and_budget():
    gear = FakeGear()
    chat = ConversationService(
        FakeExtractor([]), gear, SessionStore(1800), data_source="demo",
        today=lambda: date(2026, 11, 2),
    )

    async def scenario():
        opening = await chat.chat(ChatRequest(message="Find a car"), None)
        area = await chat.chat(ChatRequest(
            message="in duabi", session_id=opening.session_id,
        ), None)
        assert area.applied_filters.location == "Dubai"
        dated = await chat.chat(ChatRequest(
            message="4 to 8 nov", session_id=opening.session_id,
        ), None)
        assert dated.applied_filters.from_date == date(2026, 11, 4)
        assert dated.applied_filters.to_date == date(2026, 11, 8)
        priced = await chat.chat(ChatRequest(
            message="like 250 per day", session_id=opening.session_id,
        ), None)
        assert priced.applied_filters.max_price == 250
        calls_after_search = len(gear.calls)
        for wording in (
            "i want to change location",
            "can i change my location?",
            "i want to update my location",
        ):
            changed = await chat.chat(ChatRequest(
                message=wording, session_id=opening.session_id,
            ), None)
            assert changed.status == "clarification"
            assert "area" in changed.message.lower()
            assert changed.applied_filters.location == "Dubai"
            assert changed.applied_filters.from_date == date(2026, 11, 4)
            assert changed.applied_filters.max_price == 250
            assert len(gear.calls) == calls_after_search
        moved = await chat.chat(ChatRequest(
            message="actually Sharjah", session_id=opening.session_id,
        ), None)
        assert moved.applied_filters.location == "Sharjah"
        assert moved.applied_filters.from_date == date(2026, 11, 4)
        assert moved.applied_filters.max_price == 250

    asyncio.run(scenario())


def test_other_fields_follow_the_same_change_rules():
    gear = FakeGear()
    chat = ConversationService(
        FakeExtractor([
            extraction(action="recall", question_field="budget"),
            extraction(action="booking"),
        ]),
        gear, SessionStore(1800), data_source="demo", today=lambda: date(2026, 11, 2),
    )

    async def scenario():
        opening = await chat.chat(ChatRequest(message="Find a car"), None)
        await chat.chat(ChatRequest(message="Dubai", session_id=opening.session_id), None)
        await chat.chat(ChatRequest(message="4 to 8 nov", session_id=opening.session_id), None)
        await chat.chat(ChatRequest(
            message="Show options without a limit", session_id=opening.session_id,
        ), None)
        priced = await chat.chat(ChatRequest(
            message="like 250 per day", session_id=opening.session_id,
        ), None)
        assert priced.applied_filters.max_price == 250
        dates = await chat.chat(ChatRequest(
            message="change the dates", session_id=opening.session_id,
        ), None)
        assert dates.status == "clarification"
        assert dates.applied_filters.location == "Dubai"
        assert dates.applied_filters.max_price == 250
        replaced = await chat.chat(ChatRequest(
            message="change the dates to 10 to 12 nov", session_id=opening.session_id,
        ), None)
        assert replaced.applied_filters.from_date == date(2026, 11, 10)
        assert replaced.applied_filters.to_date == date(2026, 11, 12)
        assert replaced.applied_filters.location == "Dubai"
        typed = await chat.chat(ChatRequest(
            message="make it a sedan", session_id=opening.session_id,
        ), None)
        assert typed.applied_filters.car_type_name == "Sedan"
        assert typed.applied_filters.location == "Dubai"
        remembered = await chat.chat(ChatRequest(
            message="what budget did I give?", session_id=opening.session_id,
        ), None)
        assert "250" in remembered.message
        assert remembered.applied_filters.max_price == 250
        policy = await chat.chat(ChatRequest(
            message="what is the cancellation policy?", session_id=opening.session_id,
        ), None)
        assert "cancellation-policy" in policy.message
        assert policy.applied_filters.location == "Dubai"
        unclear = await chat.chat(ChatRequest(
            message="change it", session_id=opening.session_id,
        ), None)
        assert "which detail" in unclear.message.lower()
        booking = await chat.chat(ChatRequest(
            message="book this", session_id=opening.session_id,
        ), None)
        assert "sign in" in booking.message.lower()
        assert booking.applied_filters.car_type_name == "Sedan"
        reset = await chat.chat(ChatRequest(
            message="clear everything", session_id=opening.session_id,
        ), None)
        assert reset.applied_filters.location is None
        assert reset.applied_filters.max_price is None

    asyncio.run(scenario())


def test_model_revision_without_a_value_does_not_search_again():
    gear = FakeGear()
    chat = ConversationService(FakeExtractor([
        extraction(location="in duabi", max_price=250),
        extraction(action="revise", target="location"),
    ]), gear, SessionStore(1800))

    async def scenario():
        first = await chat.chat(ChatRequest(message="sedans in duabi"), None)
        assert first.applied_filters.location == "Dubai"
        calls = len(gear.calls)
        changed = await chat.chat(ChatRequest(
            message="please look somewhere else", session_id=first.session_id,
        ), None)
        assert changed.status == "clarification"
        assert changed.applied_filters.location == "Dubai"
        assert changed.applied_filters.max_price == 250
        assert len(gear.calls) == calls

    asyncio.run(scenario())


def test_language_eval_includes_typos_and_roman_urdu():
    from scripts.eval_language import PROMPTS

    messages = dict(PROMPTS)
    assert "wat area" in messages["typo_area"]
    assert "dubai mein" in messages["roman_urdu"]
    assert "cancelation" in messages["cancelation"]
    actions = no_results_actions(
        SearchFilters(location="Dubai"), anywhere=False, location_locked=True,
    )
    assert "Increase budget" not in actions
    assert "Set budget" in actions
    assert "Choose area" not in actions
    assert "Change area" in actions
