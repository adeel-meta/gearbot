"""Multi-turn rental conversations keep their task, facts, and search scope."""

import asyncio
from datetime import date

import httpx
import pytest

from app.api.v1.chat import get_chat_service
from app.main import app
from app.schemas.chat import ChatRequest, SearchFilters, VehicleCard
from app.security.sessions import SessionStore
from app.services.conversation import ConversationService, TurnConflict
from tests.test_chat_flow import FakeExtractor, FakeGear, extraction
from tests.test_extraction_fallback import FailingExtractor


TODAY = lambda: date(2026, 10, 15)


async def say(chat, message, session_id=None, *, context=None, turn_id=None):
    return await chat.chat(ChatRequest(
        message=message, session_id=session_id, context=context or {}, turn_id=turn_id,
    ), None)


def offline_chat(cards=None):
    gear = FakeGear(cards)
    extractor = FailingExtractor()
    chat = ConversationService(extractor, gear, SessionStore(1800),
                               data_source="demo", today=TODAY)
    return chat, gear, extractor


def test_reported_dates_first_conversation_waits_for_area_and_answers_memory_question():
    chat, gear, extractor = offline_chat()

    async def scenario():
        first = await say(chat, "Find a car")
        dates = await say(chat, "20 TO 22 OCT", first.session_id)
        memory = await say(chat, "WHAT AREA DID I SAID ?", first.session_id)
        area = await say(chat, "Dubai Marina", first.session_id)
        assert gear.calls == []
        result = await say(chat, "Show options without a limit", first.session_id)
        assert first.status == dates.status == area.status == "clarification"
        assert "area" in first.message.lower()
        assert "20" in dates.message and "area" in dates.message.lower()
        assert len(gear.calls) == 1
        assert memory.status == "guidance"
        assert "haven't specified an area" in memory.message.lower()
        assert memory.applied_filters.from_date == date(2026, 10, 20)
        assert area.applied_filters.location == "Dubai Marina"
        assert "budget" in area.message.lower()
        assert result.status == "results"
        assert "2026-10-20 to 2026-10-22" in result.message
        assert gear.calls[0][0].location == "Dubai Marina"
        assert "Increase budget" not in result.suggestions
        assert "Nearby areas" not in result.suggestions
        assert "Change area" in result.suggestions
        assert extractor.calls == 0

    asyncio.run(scenario())


def test_question_and_correction_do_not_turn_a_mentioned_place_into_a_search():
    chat, gear, _ = offline_chat()

    async def scenario():
        first = await say(chat, "Find a car")
        question = await say(chat, "Did I say Dubai?", first.session_id)
        assert question.applied_filters.location is None
        assert gear.calls == []
        await say(chat, "20 to 22 October", first.session_id)
        area = await say(chat, "Dubai", first.session_id)
        answer = await say(chat, "What area did I say?", first.session_id)
        assert "you said dubai" in answer.message.lower()
        assert "budget" in area.message.lower()
        correction = await say(chat, "Actually Sharjah", first.session_id)
        assert correction.applied_filters.location == "Sharjah"
        assert "budget" in correction.message.lower()
        assert gear.calls == []

    asyncio.run(scenario())


def test_budget_question_during_collection_preserves_pending_amount():
    chat, gear, extractor = offline_chat()

    async def scenario():
        first = await say(chat, "Find a car")
        await say(chat, "Dubai", first.session_id)
        budget_prompt = await say(chat, "20 to 22 October", first.session_id)
        assert "budget" in budget_prompt.message.lower()
        question = await say(chat, "What budget did I give?", first.session_id)
        assert "haven't provided a budget" in question.message.lower()
        result = await say(chat, "AED 500 per day", first.session_id)
        assert result.status == "results"
        assert "20–22 October 2026" in result.message
        assert result.applied_filters.max_price == 500
        assert result.applied_filters.budget_period == "day"
        assert gear.calls[0][0].location == "Dubai"
        assert extractor.calls == 0

    asyncio.run(scenario())


def test_trip_budget_is_not_sent_as_a_daily_price_filter():
    chat, gear, _ = offline_chat()

    async def scenario():
        first = await say(chat, "Find a car")
        await say(chat, "Dubai", first.session_id)
        await say(chat, "20 to 22 October", first.session_id)
        result = await say(chat, "AED 800 for the whole trip", first.session_id)
        assert result.status == "results"
        assert result.applied_filters.max_price is None
        assert gear.calls[0][0].max_price is None
        assert "not used as a daily price limit" in result.message
        budget = await say(chat, "What budget did I give?", first.session_id)
        assert "whole trip" in budget.message
        assert "AED 800" in budget.message

    asyncio.run(scenario())


def test_budget_period_is_clarified_before_guided_search():
    gear = FakeGear()
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai", from_date="2026-10-20",
                   to_date="2026-10-22", max_price=500),
    ]), gear, SessionStore(1800), data_source="demo", today=TODAY)

    async def scenario():
        prompt = await say(chat, "Find a car in Dubai 20 to 22 October under AED 500")
        assert prompt.status == "clarification"
        assert "per day or for the whole trip" in prompt.message
        assert gear.calls == []
        result = await say(chat, "Per day", prompt.session_id)
        assert result.status == "results"
        assert gear.calls[0][0].budget_period == "day"

    asyncio.run(scenario())


def test_refinement_buttons_ask_for_values_and_preserve_existing_criteria():
    card = VehicleCard(id="507f1f77bcf86cd799439011", title="Verified car", price=400)
    chat, gear, _ = offline_chat([card])

    async def scenario():
        first = await say(chat, "Find a car")
        await say(chat, "Dubai", first.session_id)
        await say(chat, "20 to 22 October", first.session_id)
        result = await say(chat, "AED 500 per day", first.session_id)
        assert result.status == "results"
        assert len(gear.calls) == 1
        change = await say(chat, "Change dates", first.session_id)
        assert change.status == "clarification"
        assert len(gear.calls) == 1
        updated = await say(chat, "24 to 26 October", first.session_id)
        assert updated.applied_filters.location == "Dubai"
        assert updated.applied_filters.max_price == 500
        assert updated.applied_filters.from_date == date(2026, 10, 24)
        assert len(gear.calls) == 2
        area = await say(chat, "Nearby areas", first.session_id)
        assert area.status == "clarification"
        assert "which city or area" in area.message.lower()
        assert len(gear.calls) == 2

    asyncio.run(scenario())


def test_cancellation_of_pending_change_precedes_booking_policy_faq():
    chat, gear, _ = offline_chat()

    async def scenario():
        first = await say(chat, "Find a car")
        await say(chat, "Dubai", first.session_id)
        await say(chat, "20 to 22 October", first.session_id)
        cancelled = await say(chat, "Cancel that change", first.session_id)
        assert "kept your existing search details" in cancelled.message.lower()
        assert "cancellation-policy" not in cancelled.message
        policy = await say(chat, "What is the cancellation policy?", first.session_id)
        assert "cancellation-policy" in policy.message
        assert gear.calls == []

    asyncio.run(scenario())


def test_shared_coordinates_are_not_described_as_a_named_area():
    chat, _, _ = offline_chat()
    session = chat.sessions.get_or_create(None)
    session.filters = SearchFilters(lat=25.2, lng=55.3)
    session.location_source = "shared"

    response = asyncio.run(say(chat, "What area did I say?", session.public_token))

    assert "location you shared" in response.message
    assert "haven't named an area" in response.message


def test_stale_option_number_is_not_reused_after_criteria_change():
    chat, _, _ = offline_chat()
    session = chat.sessions.get_or_create(None)
    card = VehicleCard(id="507f1f77bcf86cd799439011", title="Old result", price=300)
    session.results = [card]
    session.results_revision = 1
    session.criteria_revision = 2
    session.selected_vehicle_id = card.id

    response = asyncio.run(say(chat, "Price details", session.public_token))

    assert response.status == "clarification"
    assert "which car" in response.message.lower()
    assert response.vehicles == []


def test_unknown_or_expired_session_explains_lost_context_once():
    chat, _, _ = offline_chat()

    async def scenario():
        response = await say(chat, "Find a car", "A" * 22)
        assert "couldn't restore your previous conversation" in response.message.lower()
        following = await say(chat, "What area did I say?", response.session_id)
        assert "couldn't restore" not in following.message.lower()
        assert "haven't specified" in following.message.lower()

    asyncio.run(scenario())


def test_duplicate_turn_runs_one_search_and_reusing_id_for_other_text_is_rejected():
    chat, gear, _ = offline_chat()

    async def scenario():
        first = await say(chat, "Find a car")
        await say(chat, "Dubai", first.session_id)
        await say(chat, "20 to 22 October", first.session_id)
        replies = await asyncio.gather(*[
            say(chat, "Show options without a limit", first.session_id,
                turn_id="turn_same_request_001") for _ in range(2)
        ])
        assert replies[0] == replies[1]
        assert len(gear.calls) == 1
        with pytest.raises(TurnConflict):
            await say(chat, "Change dates", first.session_id,
                      turn_id="turn_same_request_001")
        assert len(gear.calls) == 1

    asyncio.run(scenario())


def test_http_api_reports_reused_turn_id_as_conflict():
    chat, _, _ = offline_chat()

    async def override_service():
        return chat

    app.dependency_overrides[get_chat_service] = override_service

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post("/api/v1/agent/chat", json={
                "message": "Find a car", "turn_id": "turn_same_request_002",
            })
            assert first.status_code == 200
            conflict = await client.post("/api/v1/agent/chat", json={
                "message": "Change dates", "turn_id": "turn_same_request_002",
                "session_id": first.json()["session_id"],
            })
            assert conflict.status_code == 409

    try:
        asyncio.run(scenario())
    finally:
        app.dependency_overrides.clear()

