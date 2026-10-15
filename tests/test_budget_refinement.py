"""Budget refinements must ask for a value before repeating a search."""

import asyncio

import pytest

from app.schemas.chat import ChatRequest
from app.security.sessions import SessionStore
from app.services.conversation import ConversationService
from tests.test_chat_flow import FakeExtractor, FakeGear, extraction


@pytest.mark.parametrize("initial_budget", [None, 500])
@pytest.mark.parametrize("message", [
    "Increase budget",
    "Change budget",
    "Could you raise the amount?",
])
def test_budget_change_without_amount_asks_then_accepts_follow_up(initial_budget, message):
    first_extraction = {"location": "Dubai"}
    if initial_budget is not None:
        first_extraction["max_price"] = initial_budget
    gear = FakeGear()
    chat = ConversationService(FakeExtractor([
        extraction(**first_extraction), extraction(), extraction(),
    ]), gear, SessionStore(1800))

    async def scenario():
        first = await chat.chat(ChatRequest(message="Cars in Dubai"), None)
        assert first.status == "results"
        assert len(gear.calls) == 1

        clarification = await chat.chat(ChatRequest(
            message=message, session_id=first.session_id,
        ), None)
        assert clarification.status == "clarification"
        assert "maximum budget" in clarification.message.lower()
        assert clarification.applied_filters.max_price == initial_budget
        assert len(gear.calls) == 1

        updated = await chat.chat(ChatRequest(
            message="AED 800 per day", session_id=first.session_id,
        ), None)
        assert updated.status == "results"
        assert updated.applied_filters.max_price == 800
        assert updated.applied_filters.location == "Dubai"
        assert len(gear.calls) == 2
        assert gear.calls[-1][0].max_price == 800

    asyncio.run(scenario())


@pytest.mark.parametrize("message", [
    "Increase budget to AED 800",
    "Change my budget to 900",
    "Increase my spending limit to AED 1,000",
])
def test_budget_change_with_amount_searches_immediately(message):
    gear = FakeGear()
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai", max_price=500), extraction(),
    ]), gear, SessionStore(1800))

    async def scenario():
        first = await chat.chat(ChatRequest(message="Cars in Dubai under AED 500"), None)
        updated = await chat.chat(ChatRequest(
            message=message, session_id=first.session_id,
        ), None)

        expected = 1000 if "1,000" in message else 900 if "900" in message else 800
        assert updated.status == "results"
        assert updated.applied_filters.max_price == expected
        assert len(gear.calls) == 2
        assert gear.calls[-1][0].max_price == expected

    asyncio.run(scenario())


def test_budget_change_by_amount_still_asks_for_the_new_maximum():
    gear = FakeGear()
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai", max_price=500), extraction(),
    ]), gear, SessionStore(1800))

    async def scenario():
        first = await chat.chat(ChatRequest(message="Cars in Dubai under AED 500"), None)
        clarification = await chat.chat(ChatRequest(
            message="Increase budget by AED 100", session_id=first.session_id,
        ), None)

        assert clarification.status == "clarification"
        assert "maximum budget" in clarification.message.lower()
        assert clarification.applied_filters.max_price == 500
        assert len(gear.calls) == 1

    asyncio.run(scenario())


def test_repeated_old_budget_from_extractor_does_not_skip_clarification():
    gear = FakeGear()
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai", max_price=500),
        extraction(max_price=500),
    ]), gear, SessionStore(1800))

    async def scenario():
        first = await chat.chat(ChatRequest(message="Cars in Dubai under AED 500"), None)
        clarification = await chat.chat(ChatRequest(
            message="Increase budget", session_id=first.session_id,
        ), None)

        assert clarification.status == "clarification"
        assert "maximum budget" in clarification.message.lower()
        assert clarification.applied_filters.max_price == 500
        assert len(gear.calls) == 1

    asyncio.run(scenario())


def test_budget_clarification_does_not_replace_an_existing_date_question():
    gear = FakeGear()
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai", needs_dates=True),
        extraction(),
        extraction(needs_dates=True),
        extraction(from_date="2026-10-20", to_date="2026-10-22"),
    ]), gear, SessionStore(1800), data_source="demo")

    async def scenario():
        first = await chat.chat(ChatRequest(message="Cars in Dubai next weekend"), None)
        assert first.status == "clarification"
        assert "date" in first.message.lower()

        budget = await chat.chat(ChatRequest(
            message="Increase budget", session_id=first.session_id,
        ), None)
        assert budget.status == "clarification"
        assert "maximum budget" in budget.message.lower()
        assert chat.sessions.get_or_create(first.session_id).pending_requirement == "dates"

        amount = await chat.chat(ChatRequest(
            message="AED 800", session_id=first.session_id,
        ), None)
        assert amount.status == "clarification"
        assert "date" in amount.message.lower()
        assert amount.applied_filters.max_price == 800
        assert len(gear.calls) == 0

        dates = await chat.chat(ChatRequest(
            message="20 to 22 October 2026", session_id=first.session_id,
        ), None)
        assert dates.status == "results"
        assert dates.applied_filters.max_price == 800
        assert dates.applied_filters.from_date.isoformat() == "2026-10-20"
        assert dates.applied_filters.to_date.isoformat() == "2026-10-22"
        assert len(gear.calls) == 1

    asyncio.run(scenario())
