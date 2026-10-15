import asyncio
import json
from datetime import date

import httpx

from app.api.v1.chat import get_chat_service
from app.clients.demo import DemoExtractor, DemoGearClient
from app.clients.llm import OpenAIRequirementExtractor
from app.config import Settings
from app.main import app
from app.schemas.chat import ChatRequest
from app.security.sessions import SessionStore
from app.services.conversation import ConversationService


def demo_service():
    return ConversationService(DemoExtractor(), DemoGearClient(), SessionStore(1800),
                               data_source="demo", today=lambda: date(2026, 10, 6))


def test_demo_conversation_refines_filters_and_uses_only_sample_inventory():
    service = demo_service()

    async def scenario():
        messages = [
            "I need a family car for the weekend",
            "Dubai Marina",
            "2026-10-10 to 2026-10-12",
            "Under AED 500",
            "View option 1",
            "Is option 1 available from 2026-10-10 10:00 to 2026-10-12 18:00?",
        ]
        results = []
        session_id = None
        for message in messages:
            result = await service.chat(ChatRequest(message=message, session_id=session_id), None)
            results.append(result)
            session_id = result.session_id
        assert [result.status for result in results] == [
            "clarification", "clarification", "results", "results", "details",
            "availability",
        ]
        assert all(result.data_source == "demo" for result in results)
        assert results[2].vehicles[0].title.startswith("Demo ")
        assert results[3].applied_filters.location == "Dubai Marina"
        assert results[3].applied_filters.max_price == 500
        assert results[5].available is True
        assert "In this demo" in results[5].message

    asyncio.run(scenario())


def test_demo_guardrail_blocks_unverified_filter_before_sample_search():
    result = asyncio.run(demo_service().chat(
        ChatRequest(message="Show 7-seaters with a driver in Dubai"), None))
    assert result.status == "clarification"
    assert result.vehicles == []
    assert result.data_source == "demo"


def test_demo_rating_featured_and_price_sort_use_sample_attributes():
    result = asyncio.run(demo_service().chat(ChatRequest(
        message="Show featured cars rated at least 4.6 stars, cheapest first",
    ), None))
    assert result.status == "results"
    assert result.applied_filters.min_rating == 4.6
    assert result.applied_filters.featured is True
    assert result.applied_filters.sort_by == "price_low"
    assert [car.price for car in result.vehicles] == [240, 420]


def test_hosted_extraction_can_search_sample_inventory_without_gear_access():
    settings = Settings(
        openai_model="gpt-6-luna", openai_api_key="test-key",
        gear_api_base_url="", gear_api_prefix="/Gear/api",
        request_timeout=3, session_ttl_seconds=1800,
    )

    def handler(request):
        assert request.url.host == "api.openai.com"
        payload = {
            "intent": "search", "selected_index": None, "reset_filters": False,
            "needs_dates": False, "needs_location": False,
            "unsupported_constraints": [], "location": "Dubai", "search": "SUV",
            "from_date": None, "to_date": None, "from_time": None, "to_time": None,
            "min_price": None, "max_price": 500,
        }
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(payload),
        }}]})

    service = ConversationService(
        OpenAIRequirementExtractor(settings, httpx.MockTransport(handler)),
        DemoGearClient(), SessionStore(1800), data_source="demo",
    )
    result = asyncio.run(service.chat(ChatRequest(message="SUVs in Dubai under AED 500"), None))
    assert result.status == "results"
    assert result.data_source == "demo"
    assert result.vehicles
    assert all(card.title.startswith("Demo ") for card in result.vehicles)


def test_playground_and_chat_are_accessible_through_the_app():
    service = demo_service()

    async def override():
        return service

    app.dependency_overrides[get_chat_service] = override

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            page = await client.get("/playground")
            assert page.status_code == 200
            assert "Vehicle search assistant" in page.text
            assert "sessionStorage" in page.text
            assert "Refreshing keeps this conversation" in page.text
            metadata = await client.get("/api/v1/agent/meta")
            assert metadata.status_code == 200
            assert "openai_api_key" not in metadata.text
            assert metadata.json()["llm_provider"] == "openai"
            response = await client.post("/api/v1/agent/chat",
                                         json={"message": "Show cars in Dubai under AED 500"})
            assert response.status_code == 200
            assert response.json()["data_source"] == "demo"
            assert response.json()["vehicles"]

    try:
        asyncio.run(scenario())
    finally:
        app.dependency_overrides.clear()
