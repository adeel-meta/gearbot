"""Dated searches still show Dubai listings when availability or title wording matches nothing."""

import asyncio
from datetime import date

from app.schemas.chat import ChatRequest, VehicleCard
from app.security.sessions import SessionStore
from app.services.conversation import ConversationService
from tests.test_chat_flow import FakeExtractor, FakeGear, extraction


HONDA = VehicleCard(
    id="507f1f77bcf86cd799439011", title="Honda", location="Dubai - AE",
    car_type_name="Sedan", price=25,
)


class EmptyAvailability(FakeGear):
    async def search_available_cars(self, filters, token):
        self.calls.append(("available", filters, token))
        return []


def test_empty_availability_falls_back_to_current_listings():
    gear = EmptyAvailability([HONDA])
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai", car_type_name="Sedan", search="luxury",
                   from_date="2026-11-12", to_date="2026-11-16", max_price=800),
    ]), gear, SessionStore(1800), today=lambda: date(2026, 11, 2))

    response = asyncio.run(chat.chat(
        ChatRequest(message="Show me luxury cars in Dubai from 12 to 16 November"),
        "customer-token",
    ))

    assert response.status == "results"
    assert response.vehicles[0].id == HONDA.id
    assert "couldn't confirm availability" in response.message
    assert any(call[0] == "available" for call in gear.calls)


def test_title_wording_does_not_drop_cars_of_the_requested_type():
    gear = FakeGear([HONDA])
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai", car_type_name="Sedan", search="Sedan",
                   from_date="2026-11-12", to_date="2026-11-16", max_price=800),
    ]), gear, SessionStore(1800), today=lambda: date(2026, 11, 2))

    response = asyncio.run(chat.chat(
        ChatRequest(message="sedan in Dubai 12 to 16 November under 800"),
        "customer-token",
    ))

    assert response.status == "results"
    assert response.vehicles[0].title == "Honda"
    assert "couldn't find a verified match" not in response.message
