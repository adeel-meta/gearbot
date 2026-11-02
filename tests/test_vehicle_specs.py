import asyncio

from app.schemas.chat import ChatRequest, VehicleCard
from app.security.sessions import SessionStore
from app.services.conversation import ConversationService
from tests.test_chat_flow import FakeExtractor, FakeGear, extraction


def test_seat_question_uses_selected_car_not_generic_guidance():
    urus = VehicleCard(
        id="507f1f77bcf86cd799439011", title="Lamborghini Urus", location="Dubai", seats=5,
    )
    gear = FakeGear([urus])
    service = ConversationService(
        FakeExtractor([
            extraction(location="Dubai"),
            extraction(intent="details", selected_index=1),
            extraction(intent="vehicle_specs", selected_index=1),
        ]),
        gear,
        SessionStore(1800),
    )

    async def scenario():
        first = await service.chat(ChatRequest(message="Cars in Dubai"), None)
        await service.chat(ChatRequest(message="View option 1", session_id=first.session_id), None)
        answer = await service.chat(
            ChatRequest(message="how many seat this car has?", session_id=first.session_id),
            None,
        )
        assert answer.status == "details"
        assert "5 seats" in answer.message
        assert "find a rental" not in answer.message.lower()

    asyncio.run(scenario())


def test_greeting_gets_opening_not_generic_guidance():
    service = ConversationService(FakeExtractor([extraction()]), FakeGear(), SessionStore(1800))
    response = asyncio.run(service.chat(ChatRequest(message="hello"), None))
    assert response.status == "guidance"
    assert "find the right rental" in response.message
    assert response.suggestions


def test_greeting_with_a_request_keeps_the_search():
    service = ConversationService(
        FakeExtractor([extraction(location="Dubai")]), FakeGear(), SessionStore(1800),
    )
    response = asyncio.run(service.chat(
        ChatRequest(message="Hi, I need a car in Dubai"), None,
    ))
    assert response.applied_filters.location == "Dubai"
    assert "find the right rental" not in response.message.lower()


def test_greeting_resumes_a_pending_location_question():
    service = ConversationService(FakeExtractor([]), FakeGear(), SessionStore(1800))

    async def scenario():
        first = await service.chat(ChatRequest(message="Find a car"), None)
        assert first.status == "clarification"
        resumed = await service.chat(
            ChatRequest(message="hello", session_id=first.session_id,
                        context={"display_name": "Bilal DB"}),
            None,
        )
        assert "Bilal" in resumed.message
        assert "area" in resumed.message.lower() or "where" in resumed.message.lower()
        assert "find the right rental" not in resumed.message.lower()

    asyncio.run(scenario())


def test_catalog_lookup_is_cached_for_the_signed_in_session():
    gear = FakeGear()
    service = ConversationService(FakeExtractor([
        extraction(location="Dubai", car_type_name="SUV"),
        extraction(brand_name="Toyota"),
    ]), gear, SessionStore(1800))

    async def scenario():
        first = await service.chat(
            ChatRequest(message="Show SUVs in Dubai"), "customer-token",
        )
        assert first.status == "results"
        second = await service.chat(
            ChatRequest(message="Toyota", session_id=first.session_id), "customer-token",
        )
        assert second.applied_filters.brand_id == "507f1f77bcf86cd799439013"
        assert [call for call in gear.calls if call[0] == "catalog"] == [("catalog", "customer-token")]

    asyncio.run(scenario())
