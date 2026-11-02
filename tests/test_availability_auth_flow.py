"""Regression coverage for protected reads, sign-in continuity, and rental dates."""

import asyncio
from datetime import date

import httpx
import pytest

from app.api.v1.chat import get_chat_service
from app.clients.gear import GearAuthorizationRequired, GearClient
from app.main import app
from app.schemas.chat import ChatRequest, VehicleCard
from app.security.sessions import SessionStore
from app.services.conversation import ConversationService
from tests.test_chat_flow import FakeExtractor, FakeGear, extraction
from tests.test_clients import settings


CARD = VehicleCard(
    id="507f1f77bcf86cd799439011", title="Verified Gear car", location="Dubai",
)
TODAY = lambda: date(2026, 10, 6)


@pytest.mark.parametrize("status_code", [401, 403])
@pytest.mark.parametrize("operation", ["search", "details", "check"])
def test_protected_gear_rejection_returns_sign_in_without_losing_search_state(
    status_code, operation,
):
    def gear_response(request):
        if request.url.path.endswith("/getCars"):
            return httpx.Response(200, json={"data": {"cars": [
                {"_id": CARD.id, "title": CARD.title},
            ]}})
        return httpx.Response(status_code)

    gear = GearClient(settings(), httpx.MockTransport(gear_response))
    if operation == "search":
        extractions = [extraction(from_date="2026-10-10", to_date="2026-10-12")]
    else:
        extractions = [extraction(location="Dubai"), extraction(
            intent="details" if operation == "details" else "availability",
            selected_index=1, from_date="2026-10-10", to_date="2026-10-12",
            from_time="10:00", to_time="18:00",
        )]
    chat = ConversationService(FakeExtractor(extractions), gear, SessionStore(1800),
                               today=TODAY)

    async def scenario():
        if operation == "search":
            response = await chat.chat(ChatRequest(message="Cars 10 to 12 October"), "expired")
        else:
            first = await chat.chat(ChatRequest(message="Cars in Dubai"), None)
            response = await chat.chat(ChatRequest(
                message="Option 1 details" if operation == "details" else
                        "Is option 1 available 10 to 12 October, 10:00 to 18:00?",
                session_id=first.session_id,
            ), "expired")
        session = chat.sessions.get_or_create(response.session_id)
        assert response.status == "authentication_required"
        assert response.action and response.action.type == "SIGN_IN"
        assert "can't search right now" not in response.message.lower()
        assert session.pending_operation is not None
        assert session.filters.from_date == date(2026, 10, 10)
        if operation != "search":
            assert [car.id for car in session.results] == [CARD.id]

    asyncio.run(scenario())


def test_guest_relative_date_search_resumes_automatically_after_sign_in():
    gear = FakeGear([CARD])
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai", max_price=500, needs_dates=True),
    ]), gear, SessionStore(1800), today=lambda: date(2026, 10, 1))

    async def scenario():
        guest = await chat.chat(ChatRequest(
            message="Cars in Dubai under AED 500 from 5 to 8 of this month",
        ), None)
        assert guest.status == "authentication_required"
        assert guest.action and guest.action.type == "SIGN_IN"
        assert guest.applied_filters.location == "Dubai"
        assert guest.applied_filters.max_price == 500
        assert guest.applied_filters.from_date == date(2026, 10, 5)
        assert guest.applied_filters.to_date == date(2026, 10, 8)
        assert gear.calls == []

        resumed = await chat.chat(ChatRequest(
            message="I signed in", session_id=guest.session_id,
        ), "valid-user-token")
        assert resumed.status == "results"
        assert resumed.session_id == guest.session_id
        assert [car.id for car in resumed.vehicles] == [CARD.id]
        assert resumed.applied_filters.location == "Dubai"
        assert resumed.applied_filters.max_price == 500
        assert gear.calls[0][1] == "valid-user-token"
        assert gear.calls[0][0].from_date == date(2026, 10, 5)
        assert chat.sessions.get_or_create(guest.session_id).pending_operation is None

    asyncio.run(scenario())


def test_http_guest_session_resumes_pending_availability_with_bearer_header():
    gear = FakeGear([CARD])
    chat = ConversationService(FakeExtractor([
        extraction(from_date="2026-10-10", to_date="2026-10-12"),
    ]), gear, SessionStore(1800), today=TODAY)

    async def override_service():
        return chat

    app.dependency_overrides[get_chat_service] = override_service

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            guest = await client.post("/api/v1/agent/chat", json={
                "message": "Available cars 10 to 12 October",
            })
            assert guest.status_code == 200
            guest_body = guest.json()
            assert guest_body["status"] == "authentication_required"
            assert guest_body["action"]["type"] == "SIGN_IN"
            resumed = await client.post("/api/v1/agent/chat", json={
                "message": "Continue", "session_id": guest_body["session_id"],
            }, headers={"Authorization": "Bearer valid-user-token"})
            assert resumed.status_code == 200
            assert resumed.json()["status"] == "results"
            assert resumed.json()["session_id"] == guest_body["session_id"]
            assert gear.calls[0][1] == "valid-user-token"

    try:
        asyncio.run(scenario())
    finally:
        app.dependency_overrides.clear()


def test_selected_vehicle_availability_resumes_after_sign_in_without_reasking_dates():
    gear = FakeGear([CARD])
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai"),
        extraction(intent="availability", selected_index=1, needs_dates=True,
                   from_date="2026-10-10", to_date="2026-10-12",
                   from_time="10:00", to_time="18:00"),
    ]), gear, SessionStore(1800), today=TODAY)

    async def scenario():
        first = await chat.chat(ChatRequest(message="Cars in Dubai"), None)
        gated = await chat.chat(ChatRequest(
            message="Is option 1 available from 10 to 12 October, 10:00 to 18:00?",
            session_id=first.session_id,
        ), None)
        assert gated.status == "authentication_required"
        assert gated.applied_filters.from_date == date(2026, 10, 10)
        assert [car.id for car in chat.sessions.get_or_create(first.session_id).results] == [CARD.id]

        resumed = await chat.chat(ChatRequest(
            message="Continue", session_id=first.session_id,
        ), "valid-user-token")
        assert resumed.status == "availability"
        assert resumed.available is True
        assert resumed.session_id == first.session_id
        assert gear.calls[-1] == ("availability", CARD.id, "valid-user-token")

    asyncio.run(scenario())


def test_prior_options_survive_availability_search_authentication_gate():
    gear = FakeGear([CARD])
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai"),
        extraction(from_date="2026-10-10", to_date="2026-10-12"),
    ]), gear, SessionStore(1800), today=TODAY)

    async def scenario():
        first = await chat.chat(ChatRequest(message="Cars in Dubai"), None)
        gated = await chat.chat(ChatRequest(
            message="Available 10 to 12 October", session_id=first.session_id,
        ), None)
        session = chat.sessions.get_or_create(first.session_id)
        assert gated.status == "authentication_required"
        assert [car.id for car in session.results] == [CARD.id]
        assert session.filters.location == "Dubai"
        assert session.pending_operation and session.pending_operation.kind == "search_availability"
        resumed = await chat.chat(ChatRequest(
            message="Continue", session_id=first.session_id,
        ), "valid")
        assert resumed.status == "results"
        assert gear.calls[-1][1] == "valid"

    asyncio.run(scenario())


def test_rejected_token_keeps_pending_search_for_next_valid_token():
    class RejectOnceGear(FakeGear):
        async def search_available_cars(self, filters, token):
            if token == "expired":
                raise GearAuthorizationRequired()
            return await super().search_available_cars(filters, token)

    gear = RejectOnceGear([CARD])
    chat = ConversationService(FakeExtractor([
        extraction(from_date="2026-10-10", to_date="2026-10-12"),
    ]), gear, SessionStore(1800), today=TODAY)

    async def scenario():
        rejected = await chat.chat(ChatRequest(message="Cars 10 to 12 October"), "expired")
        assert rejected.status == "authentication_required"
        assert chat.sessions.get_or_create(rejected.session_id).pending_operation is not None
        resumed = await chat.chat(ChatRequest(
            message="Continue", session_id=rejected.session_id,
        ), "valid")
        assert resumed.status == "results"
        assert resumed.vehicles[0].id == CARD.id
        assert gear.calls[0][1] == "valid"

    asyncio.run(scenario())


@pytest.mark.parametrize("message, extracted", [
    ("Cars in Dubai from 5 to 8 of this month", extraction(location="Dubai", needs_dates=True)),
    ("Cars in Dubai from 5 to 8 October", extraction(
        location="Dubai", from_date="2026-10-05", to_date="2026-10-08")),
])
def test_past_pickup_date_is_rejected_before_gear_request(message, extracted):
    gear = FakeGear([CARD])
    chat = ConversationService(FakeExtractor([extracted]), gear, SessionStore(1800),
                               today=TODAY)
    response = asyncio.run(chat.chat(ChatRequest(message=message), "valid"))
    assert response.status == "clarification"
    assert "past" in response.message.lower()
    assert "iso" not in response.message.lower()
    assert response.applied_filters.location == "Dubai"
    assert response.applied_filters.from_date == date(2026, 10, 5)
    assert gear.calls == []


def test_relative_dates_next_month_roll_over_year():
    gear = FakeGear([CARD])
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai", needs_dates=True),
    ]), gear, SessionStore(1800), today=lambda: date(2026, 12, 20))
    response = asyncio.run(chat.chat(ChatRequest(
        message="Cars in Dubai from 5th to 8th of next month",
    ), None))
    assert response.status == "authentication_required"
    assert response.applied_filters.from_date == date(2027, 1, 5)
    assert response.applied_filters.to_date == date(2027, 1, 8)
    assert gear.calls == []


def test_single_relative_pickup_in_past_is_rejected_before_other_clarifications():
    gear = FakeGear([CARD])
    chat = ConversationService(FakeExtractor([
        extraction(location="Sharjah", needs_dates=True,
                   unsupported_constraints=["car_type"]),
    ]), gear, SessionStore(1800), today=TODAY)
    response = asyncio.run(chat.chat(ChatRequest(
        message="Availability from 2 of this onth for a sedan in Sharjah",
    ), None))
    assert response.status == "clarification"
    assert "past" in response.message.lower()
    assert response.applied_filters.from_date == date(2026, 10, 2)
    assert response.applied_filters.location == "Sharjah"
    assert gear.calls == []


def test_single_relative_pickup_keeps_date_and_asks_only_for_return_date():
    gear = FakeGear([CARD])
    chat = ConversationService(FakeExtractor([
        extraction(location="Sharjah", needs_dates=True),
    ]), gear, SessionStore(1800), today=TODAY)
    response = asyncio.run(chat.chat(ChatRequest(
        message="Availability from 8 of this month in Sharjah",
    ), None))
    assert response.status == "clarification"
    assert response.message == "What return date should I use?"
    assert response.applied_filters.from_date == date(2026, 10, 8)
    assert response.applied_filters.to_date is None
    assert gear.calls == []


def test_selected_car_availability_uses_dates_without_requesting_times():
    gear = FakeGear([CARD])
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai"),
        extraction(intent="availability", selected_index=1, needs_dates=True),
        extraction(from_date="2026-10-10", to_date="2026-10-12"),
    ]), gear, SessionStore(1800), today=TODAY)

    async def scenario():
        first = await chat.chat(ChatRequest(message="Cars in Dubai"), "valid-token")
        response = await chat.chat(ChatRequest(
            message="Is option 1 available from 10 to 12 of this month?",
            session_id=first.session_id,
        ), "valid-token")
        assert response.status == "availability"
        assert response.available is True
        assert "rental days" in response.message.lower()
        assert "times" not in response.message.lower()
        assert "what pickup and return dates" not in response.message.lower()
        assert "iso" not in response.message.lower()
        assert response.applied_filters.from_date == date(2026, 10, 10)
        assert response.applied_filters.to_date == date(2026, 10, 12)

        assert gear.calls[-1] == ("availability", CARD.id, "valid-token")

    asyncio.run(scenario())


def test_selected_car_survives_details_auth_prompt_and_implicit_availability_request():
    second_card = VehicleCard(id="507f1f77bcf86cd799439012", title="Second car")
    gear = FakeGear([CARD, second_card])
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai"),
        extraction(intent="details", selected_index=1),
        extraction(intent="availability", selected_index=None, needs_dates=True),
        extraction(from_date="11 October", to_date="15 October",
                   from_time="10:00", to_time="18:00", max_price=30),
    ]), gear, SessionStore(1800), today=TODAY)

    async def scenario():
        first = await chat.chat(ChatRequest(message="Cars in Dubai"), None)
        details_gate = await chat.chat(ChatRequest(
            message="View option 1", session_id=first.session_id,
        ), None)
        date_prompt = await chat.chat(ChatRequest(
            message="Can we check availability?", session_id=first.session_id,
        ), None)
        auth_gate = await chat.chat(ChatRequest(
            message="11 to 15 October, 10:00 to 18:00, AED 30 per day",
            session_id=first.session_id,
        ), None)
        available = await chat.chat(ChatRequest(
            message="Continue", session_id=first.session_id,
        ), "valid-token")

        assert details_gate.status == "authentication_required"
        assert date_prompt.status == "clarification"
        assert "dates" in date_prompt.message.lower()
        assert auth_gate.status == "authentication_required"
        assert auth_gate.vehicles[0].id == CARD.id
        assert auth_gate.applied_filters.from_date == date(2026, 10, 11)
        assert auth_gate.applied_filters.to_date == date(2026, 10, 15)
        assert auth_gate.applied_filters.max_price == 30
        assert available.status == "availability"
        assert available.available is True
        assert available.vehicles[0].id == CARD.id
        assert "rental days" in available.message.lower()
        assert available.action is None
        assert gear.calls[-1] == ("availability", CARD.id, "valid-token")

    asyncio.run(scenario())


def test_bare_days_are_enough_for_selected_car_date_availability():
    second_card = VehicleCard(id="507f1f77bcf86cd799439012", title="test monday car")
    gear = FakeGear([CARD, second_card])
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai"),
        extraction(intent="availability", selected_index=2, needs_dates=True),
    ]), gear, SessionStore(1800), today=TODAY)

    async def scenario():
        first = await chat.chat(ChatRequest(message="Cars in Dubai"), None)
        dates = await chat.chat(ChatRequest(
            message="View option 2 and its availability", session_id=first.session_id,
        ), None)
        month = await chat.chat(ChatRequest(
            message="11 TO 15", session_id=first.session_id,
        ), None)
        available = await chat.chat(ChatRequest(
            message="Continue", session_id=first.session_id,
        ), "valid-token")

        assert dates.status == "clarification"
        assert "pickup and return dates" in dates.message.lower()
        assert month.status == "authentication_required"
        assert "sign in" in month.message.lower()
        assert month.applied_filters.from_date == date(2026, 10, 11)
        assert month.applied_filters.to_date == date(2026, 10, 15)
        assert available.status == "availability"
        assert available.available is True
        assert available.vehicles[0].id == second_card.id
        assert "rental days" in available.message.lower()
        assert gear.calls[-1] == ("availability", second_card.id, "valid-token")

    asyncio.run(scenario())


def test_search_result_copy_does_not_repeat_location_or_budget():
    gear = FakeGear([CARD])
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai", max_price=500),
        extraction(max_price=800),
    ]), gear, SessionStore(1800), today=TODAY)

    async def scenario():
        first = await chat.chat(ChatRequest(message="Dubai cars under AED 500"), None)
        second = await chat.chat(ChatRequest(
            message="Increase budget to AED 800", session_id=first.session_id,
        ), None)
        assert first.status == second.status == "results"
        assert first.message.count("Dubai") == 1
        assert first.message.count("AED 500") == 1
        assert second.message.count("Dubai") == 1
        assert second.message.count("AED 800") == 1

    asyncio.run(scenario())


def test_empty_search_copy_does_not_repeat_location_or_budget():
    gear = FakeGear()
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai", max_price=500),
        extraction(max_price=800),
    ]), gear, SessionStore(1800), today=TODAY)

    async def scenario():
        first = await chat.chat(ChatRequest(message="Dubai cars under AED 500"), None)
        second = await chat.chat(ChatRequest(
            message="Increase budget to AED 800", session_id=first.session_id,
        ), None)
        assert first.status == second.status == "results"
        assert second.vehicles == []
        assert second.message.count("Dubai") == 1
        assert second.message.count("AED 800") == 1

    asyncio.run(scenario())
