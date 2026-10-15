"""Acceptance coverage for the capabilities promised to Gear users."""

import asyncio
from datetime import date

from app.schemas.chat import ChatRequest, VehicleCard
from app.security.sessions import SessionStore
from app.services.conversation import ConversationService
from tests.test_chat_flow import FakeExtractor, FakeGear, extraction


SUV_ID = "507f1f77bcf86cd799439012"
TODAY = lambda: date(2026, 10, 6)


def test_five_seater_fuel_efficient_three_day_request_keeps_duration():
    card = VehicleCard(
        id="507f1f77bcf86cd799439011", title="Verified hybrid", location="Dubai",
        seats=5, fuel_type="Hybrid", fuel_efficiency=19.5, price=250,
    )
    gear = FakeGear([card])
    chat = ConversationService(FakeExtractor([
        extraction(needs_dates=True),
        extraction(from_date="2026-10-10", from_time="10:00", to_time="10:00"),
    ]), gear, SessionStore(1800), today=TODAY)

    async def scenario():
        first = await chat.chat(ChatRequest(
            message="I need a 5-seater fuel-efficient car for 3 days.",
        ), None)
        assert first.status == "clarification"
        assert first.applied_filters.seats == 5
        assert first.applied_filters.fuel_efficiency_preferred is True
        assert first.applied_filters.rental_days == 3
        assert "start" in first.message.lower()

        second = await chat.chat(ChatRequest(
            message="Start on 10 October 2026 at 10:00.", session_id=first.session_id,
        ), "customer-token")
        assert second.status == "results"
        assert second.applied_filters.from_date.isoformat() == "2026-10-10"
        assert second.applied_filters.to_date.isoformat() == "2026-10-13"
        assert second.vehicles[0].seats == 5
        assert second.vehicles[0].fuel_efficiency == 19.5

    asyncio.run(scenario())


def test_suv_near_location_resolves_verified_catalog_name():
    card = VehicleCard(
        id="507f1f77bcf86cd799439011", title="Verified SUV",
        car_type_id=SUV_ID, car_type_name="SUV", location="Dubai",
    )
    gear = FakeGear([card])
    chat = ConversationService(FakeExtractor([
        extraction(needs_location=True, needs_dates=True, sort_by="distance"),
        extraction(from_date="2026-10-10", to_date="2026-10-12"),
    ]), gear, SessionStore(1800), today=TODAY)

    async def scenario():
        first = await chat.chat(ChatRequest(
            message="Show me available SUVs near my location.",
            context={"lat": 25.2048, "lng": 55.2708},
        ), None)
        assert first.status == "clarification"
        assert "date" in first.message.lower()
        second = await chat.chat(ChatRequest(
            message="10 to 12 October 2026", session_id=first.session_id,
        ), "customer-token")
        assert second.status == "results"
        assert second.applied_filters.car_type_id == SUV_ID
        assert second.applied_filters.lat == 25.2048
        search_call = next(call for call in gear.calls if not isinstance(call[0], str))
        assert search_call[0].car_type_id == SUV_ID

    asyncio.run(scenario())


def test_date_based_availability_post_filters_location_from_verified_cards():
    dubai = VehicleCard(id="507f1f77bcf86cd799439031", title="Dubai car", location="Dubai")
    sharjah = VehicleCard(id="507f1f77bcf86cd799439032", title="Sharjah car", location="Sharjah")
    gear = FakeGear([dubai, sharjah])
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai", from_date="2026-10-10", to_date="2026-10-12"),
    ]), gear, SessionStore(1800), today=TODAY)

    response = asyncio.run(chat.chat(
        ChatRequest(message="Show available cars in Dubai from 10 to 12 October 2026"),
        "customer-token",
    ))
    assert [car.id for car in response.vehicles] == [dubai.id]


def test_family_recommendation_uses_verified_signals_for_ranking():
    compact = VehicleCard(
        id="507f1f77bcf86cd799439021", title="Compact", seats=4,
        luggage_capacity=1, location="Dubai",
    )
    family = VehicleCard(
        id="507f1f77bcf86cd799439022", title="Family option", seats=7,
        luggage_capacity=4, car_type_name="SUV", location="Dubai",
    )
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai"),
    ]), FakeGear([compact, family]), SessionStore(1800))

    response = asyncio.run(chat.chat(
        ChatRequest(message="Show cars suitable for family travel in Dubai."), None,
    ))

    assert response.status == "results"
    assert response.vehicles[0].id == family.id
    assert "family-capacity seating" in response.vehicles[0].match_reason


def test_family_request_without_location_asks_only_for_location():
    chat = ConversationService(FakeExtractor([
        extraction(),
    ]), FakeGear(), SessionStore(1800))
    response = asyncio.run(chat.chat(
        ChatRequest(message="Find a car suitable for family travel."), None,
    ))
    assert response.status == "clarification"
    assert "where" in response.message.lower()


def test_official_faq_answers_do_not_require_the_model_or_login():
    chat = ConversationService(FakeExtractor([]), FakeGear(), SessionStore(1800))
    response = asyncio.run(chat.chat(ChatRequest(message="What is the cancellation policy?"), None))
    assert response.status == "guidance"
    assert "rental partner" in response.message.lower()
    assert "gearapp.ae/cancellation-policy" in response.message


def test_catalog_lookup_waits_for_sign_in_and_resumes_same_conversation():
    card = VehicleCard(
        id="507f1f77bcf86cd799439011", title="SUV", car_type_id=SUV_ID,
        car_type_name="SUV", location="Dubai",
    )
    gear = FakeGear([card])
    chat = ConversationService(FakeExtractor([
        extraction(location="Dubai"),
        extraction(location="Dubai"),
    ]), gear, SessionStore(1800))

    async def scenario():
        guest = await chat.chat(ChatRequest(message="Show SUVs in Dubai"), None)
        assert guest.status == "authentication_required"
        resumed = await chat.chat(ChatRequest(
            message="continue", session_id=guest.session_id,
        ), "customer-token")
        assert resumed.status == "results"
        assert resumed.session_id == guest.session_id
        assert resumed.applied_filters.car_type_id == SUV_ID

    asyncio.run(scenario())
