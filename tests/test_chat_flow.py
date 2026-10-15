import asyncio
from datetime import date

import httpx
import pytest

from app.api.v1.chat import get_chat_service
from app.clients.llm import Extraction
from app.main import app
from app.schemas.chat import ChatRequest, VehicleCard
from app.security.sessions import SessionStore
from app.services.conversation import ConversationService


def extraction(**updates):
    values = {
        "intent": "search", "selected_index": None, "reset_filters": False,
        "needs_dates": False, "needs_location": False,
        "unsupported_constraints": [], "location": None, "search": None,
        "from_date": None, "to_date": None, "from_time": None, "to_time": None,
        "min_price": None, "max_price": None,
    }
    values.update(updates)
    return Extraction.model_validate(values)


class FakeExtractor:
    def __init__(self, values):
        self.values = iter(values)

    async def extract(self, message, current, result_titles):
        return next(self.values)


class FakeGear:
    def __init__(self, cards=None):
        self.calls = []
        self.cards = cards or []

    async def search_cars(self, filters, token):
        self.calls.append((filters, token))
        return self.cards

    async def search_available_cars(self, filters, token):
        self.calls.append((filters, token))
        return self.cards

    async def car_details(self, car_id, token):
        self.calls.append(("details", car_id, token))
        return next(card for card in self.cards if card.id == car_id)

    async def check_availability(self, car_id, filters, token):
        self.calls.append(("availability", car_id, token))
        return True

    async def filter_catalog(self, token):
        self.calls.append(("catalog", token))
        return {"data": {
            "car_types": [
                {"_id": "507f1f77bcf86cd799439012", "name": "SUV"},
                {"_id": "507f1f77bcf86cd799439014", "name": "Sedan"},
            ],
            "brands": [{"_id": "507f1f77bcf86cd799439013", "name": "Toyota"}],
        }}


def test_clarification_then_refinement_retains_validated_filters():
    gear = FakeGear([VehicleCard(
        id="507f1f77bcf86cd799439011", title="Family car", location="Dubai Marina",
    )])
    service = ConversationService(FakeExtractor([
        extraction(needs_location=True, needs_dates=True),
        extraction(location="Dubai Marina", needs_dates=True),
        extraction(from_date="2026-10-10", to_date="2026-10-12", max_price=700),
        extraction(max_price=500),
    ]), gear, SessionStore(1800), today=lambda: date(2026, 10, 6))

    async def scenario():
        first = await service.chat(ChatRequest(message="Family car this weekend"), "customer-a")
        assert first.status == "clarification"
        second = await service.chat(ChatRequest(message="Dubai Marina", session_id=first.session_id), "customer-a")
        assert second.status == "clarification"
        third = await service.chat(ChatRequest(message="October 10 to 12, under AED 700",
                                               session_id=first.session_id), "customer-a")
        assert third.status == "results"
        assert third.vehicles[0].id == "507f1f77bcf86cd799439011"
        fourth = await service.chat(ChatRequest(message="Under AED 500",
                                                session_id=first.session_id), "customer-a")
        assert fourth.applied_filters.location == "Dubai Marina"
        assert fourth.applied_filters.max_price == 500
        assert fourth.applied_filters.from_date.isoformat() == "2026-10-10"
        assert len(gear.calls) == 2

    asyncio.run(scenario())


def test_listing_filters_and_client_catalog_ids_reach_gear():
    type_id = "507f1f77bcf86cd799439012"
    brand_id = "507f1f77bcf86cd799439013"
    gear = FakeGear()
    service = ConversationService(FakeExtractor([
        extraction(location="Dubai", min_rating=4, featured=True, sort_by="price_low"),
    ]), gear, SessionStore(1800))

    response = asyncio.run(service.chat(ChatRequest(
        message="Show featured cars rated 4+ in Dubai, cheapest first",
        context={"car_type_id": type_id, "brand_id": brand_id},
    ), None))

    filters = gear.calls[0][0]
    assert response.status == "results"
    assert filters.car_type_id == type_id
    assert filters.brand_id == brand_id
    assert filters.min_rating == 4
    assert filters.featured is True
    assert filters.sort_by == "price_low"


def test_distance_sort_asks_for_coordinates_before_searching():
    gear = FakeGear()
    service = ConversationService(FakeExtractor([extraction(sort_by="distance")]),
                                  gear, SessionStore(1800))
    response = asyncio.run(service.chat(ChatRequest(message="Show the nearest cars"), None))
    assert response.status == "clarification"
    assert "location" in response.message
    assert gear.calls == []


def test_session_identity_is_independent_of_the_gear_user_token():
    service = ConversationService(FakeExtractor([extraction(location="Dubai"),
                                                 extraction(location="Dubai")]),
                                  FakeGear(), SessionStore(1800))

    async def scenario():
        first = await service.chat(ChatRequest(message="Cars in Dubai"), "customer-a")
        second = await service.chat(ChatRequest(message="Same session", session_id=first.session_id),
                                    "customer-b")
        assert second.session_id == first.session_id

    asyncio.run(scenario())


def test_public_search_session_is_unchanged_after_sign_in():
    card = VehicleCard(id="507f1f77bcf86cd799439011", title="Verified car")
    service = ConversationService(FakeExtractor([
        extraction(location="Dubai"),
        extraction(intent="details", selected_index=1),
    ]), FakeGear([card]), SessionStore(1800))

    async def scenario():
        public = await service.chat(ChatRequest(message="Cars in Dubai"), None)
        details = await service.chat(ChatRequest(message="Show option 1",
                                                 session_id=public.session_id), "customer-a")
        assert details.status == "details"
        session = service.sessions.get_or_create(public.session_id)
        assert session.public_token == public.session_id

    asyncio.run(scenario())


def test_unverified_driver_filter_is_blocked_even_if_model_misses_it():
    gear = FakeGear()
    service = ConversationService(FakeExtractor([extraction(location="Dubai")]),
                                  gear, SessionStore(1800))
    response = asyncio.run(service.chat(ChatRequest(message="Cars with a driver in Dubai"), None))
    assert response.status == "clarification"
    assert "driver option" in response.message
    assert gear.calls == []


@pytest.mark.parametrize("message, expected", [
    ("Cars with Bluetooth and a child seat", ("features",)),
])
def test_unverified_feature_filters_do_not_silently_return_unfiltered_cars(message, expected):
    gear = FakeGear()
    service = ConversationService(FakeExtractor([extraction(location="Dubai")]),
                                  gear, SessionStore(1800))

    response = asyncio.run(service.chat(ChatRequest(message=message), None))

    assert response.status == "clarification"
    assert all(label in response.message for label in expected)
    assert gear.calls == []


def test_named_car_type_needs_verified_catalog_id():
    gear = FakeGear()
    service = ConversationService(FakeExtractor([extraction(location="Dubai", search="SUV")]),
                                  gear, SessionStore(1800))

    response = asyncio.run(service.chat(ChatRequest(message="Show SUVs in Dubai"), None))

    assert response.status == "authentication_required"
    assert response.action.type == "SIGN_IN"
    assert gear.calls == []


def test_named_brand_needs_verified_catalog_id():
    gear = FakeGear()
    service = ConversationService(FakeExtractor([
        extraction(location="Dubai", search="Toyota", unsupported_constraints=["brand"]),
    ]), gear, SessionStore(1800))

    response = asyncio.run(service.chat(ChatRequest(message="Show Toyota cars in Dubai"), None))

    assert response.status == "authentication_required"
    assert response.action.type == "SIGN_IN"
    assert gear.calls == []


def test_validated_type_id_allows_named_type_filter():
    type_id = "507f1f77bcf86cd799439012"
    gear = FakeGear()
    service = ConversationService(FakeExtractor([
        extraction(location="Dubai", unsupported_constraints=["car_type"]),
    ]), gear, SessionStore(1800))

    response = asyncio.run(service.chat(ChatRequest(
        message="Show SUVs in Dubai", context={"car_type_id": type_id},
    ), None))

    assert response.status == "results"
    assert gear.calls[0][0].car_type_id == type_id


def test_details_and_availability_use_only_previous_gear_result_id():
    card = VehicleCard(id="507f1f77bcf86cd799439011", title="Verified car")
    gear = FakeGear([card])
    service = ConversationService(FakeExtractor([
        extraction(location="Dubai"),
        extraction(intent="details", selected_index=1),
        extraction(intent="availability", selected_index=2),
        extraction(intent="availability", selected_index=1,
                   from_date="2026-10-10", to_date="2026-10-12",
                   from_time="10:00", to_time="18:00"),
    ]), gear, SessionStore(1800), today=lambda: date(2026, 10, 6))

    async def scenario():
        first = await service.chat(ChatRequest(message="Cars in Dubai"), "customer-a")
        details = await service.chat(ChatRequest(message="Show first car",
                                                 session_id=first.session_id), "customer-a")
        assert details.status == "details"
        invalid = await service.chat(ChatRequest(message="Check option 2",
                                                 session_id=first.session_id), "customer-a")
        assert invalid.status == "clarification"
        available = await service.chat(ChatRequest(message="Is option 1 available October 10 to 12?",
                                                   session_id=first.session_id), "customer-a")
        assert available.status == "availability"
        assert available.available is True
        assert [call[1] for call in gear.calls[1:]] == [card.id, card.id]

    asyncio.run(scenario())


def test_anonymous_user_gets_sign_in_for_protected_follow_ups_and_session_continues():
    card = VehicleCard(id="507f1f77bcf86cd799439011", title="Verified car")
    gear = FakeGear([card])
    service = ConversationService(FakeExtractor([
        extraction(location="Dubai"),
        extraction(intent="details", selected_index=1),
        extraction(intent="details", selected_index=1),
        extraction(intent="availability", selected_index=1,
                   from_date="2026-10-10", to_date="2026-10-12",
                   from_time="10:00", to_time="18:00"),
        extraction(intent="availability", selected_index=1),
    ]), gear, SessionStore(1800), today=lambda: date(2026, 10, 6))

    async def scenario():
        first = await service.chat(ChatRequest(message="Cars in Dubai"), None)
        details = await service.chat(ChatRequest(message="Option 1 details",
                                                session_id=first.session_id), None)
        availability = await service.chat(ChatRequest(
            message="Is option 1 available?", session_id=first.session_id), None)
        assert details.status == "authentication_required" and details.action
        assert availability.status == "authentication_required" and availability.action
        assert "sign in" in availability.message.lower()
        signed_details = await service.chat(ChatRequest(
            message="Option 1 details", session_id=first.session_id), "customer-a")
        signed_availability = await service.chat(ChatRequest(
            message="Check option 1", session_id=first.session_id), "customer-a")
        assert signed_details.status == "details"
        assert signed_availability.status == "availability"
        assert signed_availability.available is True
        assert first.session_id == signed_details.session_id == signed_availability.session_id
        assert len(gear.calls) == 3
        assert gear.calls[0][0].location == "Dubai"
        assert [call[0] for call in gear.calls[1:]] == ["details", "availability"]

    asyncio.run(scenario())


def test_api_contract_and_authorization_header():
    service = ConversationService(FakeExtractor([extraction(location="Dubai")]),
                                  FakeGear(), SessionStore(1800))

    async def override_service():
        return service

    app.dependency_overrides[get_chat_service] = override_service

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            bad = await client.post("/api/v1/agent/chat", json={"message": " "})
            assert bad.status_code == 422
            bad_auth = await client.post("/api/v1/agent/chat", json={"message": "cars"},
                                         headers={"Authorization": "Basic abc"})
            assert bad_auth.status_code == 401
            result = await client.post("/api/v1/agent/chat", json={"message": "Cars in Dubai"},
                                       headers={"Authorization": "Bearer customer-a"})
            assert result.status_code == 200
            assert result.headers["X-Request-ID"]
            assert result.json()["status"] == "results"
            assert result.json()["applied_filters"]["location"] == "Dubai"
            assert "customer-a" not in result.text

    try:
        asyncio.run(scenario())
    finally:
        app.dependency_overrides.clear()


def test_new_chat_cookie_reset_removes_all_previous_filters():
    service = ConversationService(FakeExtractor([
        extraction(location="Dubai", max_price=500),
        extraction(location="Sharjah"),
    ]), FakeGear(), SessionStore(1800))

    async def override_service():
        return service

    app.dependency_overrides[get_chat_service] = override_service

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            first = await client.post(
                "/api/v1/agent/chat",
                json={"message": "Dubai cars under AED 500"},
                headers={"X-AI-Session-Transport": "cookie"},
            )
            reset = await client.post(
                "/api/v1/agent/session/reset",
                headers={"X-AI-Session-Transport": "cookie"},
            )
            second = await client.post(
                "/api/v1/agent/chat",
                json={"message": "Cars in Sharjah"},
                headers={"X-AI-Session-Transport": "cookie"},
            )
        assert first.json()["applied_filters"]["max_price"] == 500
        assert reset.status_code == 204
        assert second.json()["applied_filters"]["location"] == "Sharjah"
        assert "max_price" not in second.json()["applied_filters"]
        assert len(service.sessions.sessions) == 1

    try:
        asyncio.run(scenario())
    finally:
        app.dependency_overrides.clear()
