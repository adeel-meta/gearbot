"""Known date and budget answers stay usable during model outages."""

import asyncio
from datetime import date

import pytest

from app.clients.llm import LLMUnavailable
from app.schemas.chat import ChatRequest, SearchFilters, VehicleCard
from app.security.sessions import SessionStore
from app.services.conversation import ConversationService
from tests.test_chat_flow import FakeGear


class FailingExtractor:
    def __init__(self):
        self.calls = 0

    async def extract(self, message, current, result_titles):
        self.calls += 1
        raise LLMUnavailable("provider unavailable")


def test_rich_date_reply_is_not_partially_applied_when_extractor_is_down():
    sessions = SessionStore(1800)
    session = sessions.get_or_create(None)
    session.filters = SearchFilters(location="Dubai")
    session.pending_requirement = "dates"
    gear = FakeGear()
    extractor = FailingExtractor()
    chat = ConversationService(extractor, gear, sessions, data_source="demo",
                               today=lambda: date(2026, 10, 15))

    response = asyncio.run(chat.chat(ChatRequest(
        message="20 to 22 October, SUV, pickup 10:00", session_id=session.public_token,
    ), None))

    assert response.status == "unavailable"
    assert response.applied_filters.from_date is None
    assert gear.calls == []
    assert extractor.calls == 1


@pytest.mark.parametrize("message,expected_max", [
    ("18 to 20 oct", 500),
    ("18 to 20 october", 500),
    ("18 to 20 oct under AED 700", 700),
])
def test_named_month_date_follow_up_recovers_without_model(message, expected_max):
    sessions = SessionStore(1800)
    session = sessions.get_or_create(None)
    session.filters = SearchFilters(location="Dubai", max_price=500)
    session.pending_requirement = "dates"
    card = VehicleCard(id="507f1f77bcf86cd799439011", title="Verified car")
    gear = FakeGear([card])
    extractor = FailingExtractor()
    chat = ConversationService(
        extractor, gear, sessions, data_source="demo",
        today=lambda: date(2026, 10, 15),
    )

    response = asyncio.run(chat.chat(ChatRequest(
        message=message, session_id=session.public_token,
    ), None))

    assert response.status == "results"
    assert response.applied_filters.from_date == date(2026, 10, 18)
    assert response.applied_filters.to_date == date(2026, 10, 20)
    assert response.applied_filters.location == "Dubai"
    assert response.applied_filters.max_price == expected_max
    assert len(gear.calls) == 1
    assert extractor.calls == 0


def test_pending_budget_answer_recovers_without_model():
    sessions = SessionStore(1800)
    session = sessions.get_or_create(None)
    session.filters = SearchFilters(location="Dubai", max_price=500)
    session.pending_budget = True
    gear = FakeGear()
    extractor = FailingExtractor()
    chat = ConversationService(extractor, gear, sessions, data_source="demo")

    response = asyncio.run(chat.chat(ChatRequest(
        message="AED 800", session_id=session.public_token,
    ), None))

    assert response.status == "results"
    assert response.applied_filters.max_price == 800
    assert response.applied_filters.location == "Dubai"
    assert session.pending_budget is False
    assert len(gear.calls) == 1
    assert extractor.calls == 0


def test_bare_date_follow_up_asks_for_month_when_model_is_unavailable():
    sessions = SessionStore(1800)
    session = sessions.get_or_create(None)
    session.filters = SearchFilters(location="Dubai")
    session.pending_requirement = "dates"
    gear = FakeGear()
    extractor = FailingExtractor()
    chat = ConversationService(
        extractor, gear, sessions, data_source="demo",
        today=lambda: date(2026, 10, 15),
    )

    response = asyncio.run(chat.chat(ChatRequest(
        message="18 to 20", session_id=session.public_token,
    ), None))

    assert response.status == "clarification"
    assert "month and year" in response.message.lower()
    assert gear.calls == []
    assert extractor.calls == 0


def test_invalid_named_date_gets_validation_prompt_when_model_is_unavailable():
    sessions = SessionStore(1800)
    session = sessions.get_or_create(None)
    session.filters = SearchFilters(location="Dubai")
    session.pending_requirement = "dates"
    gear = FakeGear()
    extractor = FailingExtractor()
    chat = ConversationService(
        extractor, gear, sessions, data_source="demo",
        today=lambda: date(2026, 10, 15),
    )

    response = asyncio.run(chat.chat(ChatRequest(
        message="32 to 35 oct", session_id=session.public_token,
    ), None))

    assert response.status == "clarification"
    assert "aren't a valid" in response.message.lower()
    assert gear.calls == []
    assert extractor.calls == 0


def test_value_less_budget_request_asks_when_model_is_unavailable():
    sessions = SessionStore(1800)
    session = sessions.get_or_create(None)
    session.filters = SearchFilters(location="Dubai", max_price=500)
    gear = FakeGear()
    extractor = FailingExtractor()
    chat = ConversationService(extractor, gear, sessions, data_source="demo")

    response = asyncio.run(chat.chat(ChatRequest(
        message="Increase budget", session_id=session.public_token,
    ), None))

    assert response.status == "clarification"
    assert "maximum budget" in response.message.lower()
    assert response.applied_filters.max_price == 500
    assert gear.calls == []
    assert extractor.calls == 0
