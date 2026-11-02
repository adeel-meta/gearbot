"""Regressions reproduced in the November 2026 browser UAT."""

import asyncio

from app.schemas.chat import ChatRequest, VehicleCard
from app.security.sessions import SessionStore
from app.services.conversation import ConversationService
from app.services.faqs import faq_answer
from tests.test_chat_flow import FakeExtractor, FakeGear


class PriceFilteredGear(FakeGear):
    supports_public_price_fallback = True
    async def search_cars(self, filters, token):
        self.calls.append((filters, token))
        if filters.max_price is not None:
            return []
        return [VehicleCard(id="507f1f77bcf86cd799439011", title="Honda",
                            location="Dubai - AE", price=25.2)]


def test_cancelation_policy_typo_returns_policy():
    answer = faq_answer("cancelation policy")
    assert answer is not None
    assert "cancellation-policy" in answer[0]


def test_duabi_is_corrected_before_search():
    gear = PriceFilteredGear()
    chat = ConversationService(FakeExtractor([]), gear, SessionStore(1800))
    response = asyncio.run(chat.chat(ChatRequest(message="i want a car in duabi"), None))
    assert response.applied_filters.location == "Dubai"
    assert "duabi" not in response.message.casefold()


def test_cheap_car_asks_for_search_details_instead_of_all_inventory():
    chat = ConversationService(FakeExtractor([]), PriceFilteredGear(), SessionStore(1800))
    response = asyncio.run(chat.chat(ChatRequest(message="cheap car"), None))
    assert response.status == "clarification"
    assert response.applied_filters.sort_by == "price_low"
    assert not response.vehicles


def test_nonsense_does_not_return_all_inventory():
    chat = ConversationService(FakeExtractor([]), PriceFilteredGear(), SessionStore(1800))
    response = asyncio.run(chat.chat(ChatRequest(message="asdfgh"), None))
    assert response.status == "clarification"
    assert not response.vehicles


def test_public_price_filter_falls_back_to_verified_listing_price():
    gear = PriceFilteredGear()
    chat = ConversationService(FakeExtractor([]), gear, SessionStore(1800))

    async def scenario():
        first = await chat.chat(ChatRequest(message="i want a car in duabi"), None)
        # A fake extractor is sufficient here because the amount is parsed locally.
        from tests.test_chat_flow import extraction
        chat.extractor = FakeExtractor([extraction(max_price=50, budget_period="day")])
        return await chat.chat(ChatRequest(message="AED 50 per day", session_id=first.session_id), None)

    response = asyncio.run(scenario())
    assert response.status == "results"
    assert [car.title for car in response.vehicles] == ["Honda"]
    assert "checked verified daily listing prices" in response.message
