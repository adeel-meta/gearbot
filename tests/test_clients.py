import asyncio
import json
import logging
from dataclasses import replace

import httpx
import pytest

from app.clients.gear import (GearAuthorizationRequired, GearClient,
                              GearUnavailable, resolve_catalog_id, vehicle_cards)
from app.clients.llm import OpenAIRequirementExtractor, LLMUnavailable
from app.config import ConfigurationError, Settings
from app.schemas.chat import SearchFilters


def settings():
    return Settings(
        openai_model="gpt-6-luna", openai_api_key="test-openai-key",
        gear_api_base_url="https://staging.example.test", gear_api_prefix="/Gear/api",
        request_timeout=3, session_ttl_seconds=1800,
    )


def test_gear_public_search_does_not_forward_customer_token_and_maps_present_fields():
    def handler(request):
        assert str(request.url).startswith("https://staging.example.test/Gear/api/getCars?")
        assert "Authorization" not in request.headers
        assert request.url.params["location"] == "Dubai"
        return httpx.Response(200, json={"data": [{
            "_id": "507f1f77bcf86cd799439011", "title": "Real Gear title",
            "location": "Dubai", "price": 300,
        }]})

    client = GearClient(settings(), httpx.MockTransport(handler))
    cards = asyncio.run(client.search_cars(SearchFilters(location="Dubai"), "customer-token"))
    assert cards[0].title == "Real Gear title"
    assert cards[0].price == 300
    assert cards[0].rating is None


def test_public_search_retries_one_transient_connection_failure():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("temporary Gear connection failure", request=request)
        return httpx.Response(200, json={"data": {"cars": [{
            "_id": "507f1f77bcf86cd799439011", "title": "Recovered car",
        }]}})

    client = GearClient(settings(), httpx.MockTransport(handler))
    cards = asyncio.run(client.search_cars(SearchFilters(location="Dubai"), None))
    assert attempts == 2
    assert cards[0].title == "Recovered car"


def test_documented_listing_filters_are_encoded_without_model_generated_ids():
    type_id = "507f1f77bcf86cd799439012"
    brand_id = "507f1f77bcf86cd799439013"

    def handler(request):
        params = request.url.params
        assert params["car_type_id"] == type_id
        assert params["brand_id"] == brand_id
        assert params["min_rating"] == "4.0"
        assert params["featured"] == "true"
        assert params["sort_by"] == "price_low"
        return httpx.Response(200, json={"data": {"cars": []}})

    filters = SearchFilters(car_type_id=type_id, brand_id=brand_id,
                            min_rating=4, featured=True, sort_by="price_low")
    assert asyncio.run(GearClient(settings(), httpx.MockTransport(handler))
                       .search_cars(filters, None)) == []
    assert SearchFilters(car_type_ids=[type_id, brand_id]).query_params()["car_type_ids"] == f"{type_id},{brand_id}"


def test_unrecognized_gear_payload_cannot_be_presented_as_empty_inventory():
    with pytest.raises(GearUnavailable):
        vehicle_cards({"data": {"unrecognized": []}})
    with pytest.raises(GearUnavailable):
        vehicle_cards({"data": [{"title": "Car without backend ID"}]})


def test_live_rating_maps_from_avg_rating_without_forwarding_vendor_data():
    cards = vehicle_cards({"data": {"cars": [{
        "_id": "507f1f77bcf86cd799439011", "title": "Gear car",
        "avg_rating": 4.5,
        "company": [{"password": "never-include-this", "email": "vendor@example.com"}],
    }]}})

    assert cards[0].rating == 4.5
    assert "company" not in cards[0].model_dump_json()
    assert "never-include-this" not in cards[0].model_dump_json()


def test_vehicle_mapper_exposes_only_verified_recommendation_fields():
    cards = vehicle_cards({"data": {"cars": [{
        "_id": "507f1f77bcf86cd799439011", "title": "Gear car",
        "day_price_with_fee": "325", "no_seats": "5", "fuel_type": "Hybrid",
        "fuel_efficiency": "18.5 km/l", "transmission": "Automatic",
        "car_type_id": "507f1f77bcf86cd799439012", "car_type_name": "SUV",
        "luggage_capacity": 3,
    }]}})

    assert cards[0].price == 325
    assert cards[0].price_basis == "day"
    assert cards[0].seats == 5
    assert cards[0].fuel_efficiency == 18.5
    assert cards[0].car_type_name == "SUV"


def test_catalog_names_resolve_only_to_ids_returned_by_gear():
    payload = {"data": {"car_types": [
        {"_id": "507f1f77bcf86cd799439012", "name": "SUV"},
    ]}}
    assert resolve_catalog_id(payload, "car_type", "SUVs") == "507f1f77bcf86cd799439012"
    assert resolve_catalog_id(payload, "car_type", "Limousine") is None


def test_gear_details_and_availability_forward_user_credential_and_explicit_data():
    car_id = "507f1f77bcf86cd799439011"

    def handler(request):
        assert request.headers["Authorization"] == "Bearer customer-token"
        assert request.headers["Accept"] == "application/json"
        assert request.headers["Language"] == "en"
        if request.url.path.endswith("/getCarDetails"):
            assert request.url.params["car_id"] == car_id
            return httpx.Response(200, json={"data": {"_id": car_id, "title": "Gear title"}})
        assert request.url.path.endswith("/checkIsCarAvailable")
        assert json.loads(request.content)["car_id"] == car_id
        return httpx.Response(200, json={"data": {"is_available": False}})

    client = GearClient(settings(), httpx.MockTransport(handler))
    with pytest.raises(GearAuthorizationRequired):
        asyncio.run(client.car_details(car_id, None))
    details = asyncio.run(client.car_details(car_id, "customer-token"))
    assert details.title == "Gear title"
    filters = SearchFilters(from_date="2026-10-10", to_date="2026-10-12",
                            from_time="10:00", to_time="18:00")
    assert asyncio.run(client.check_availability(car_id, filters, "customer-token")) is False


@pytest.mark.parametrize(("payload", "expected"), [
    ({"status": True, "message": "Car is available"}, True),
    ({"status": False, "message": "Car is not available"}, False),
    ({"status": True, "data": True}, True),
    ({"status": True, "data": {"isAvailable": False}}, False),
    ({"message": "Available"}, True),
])
def test_availability_matches_gear_web_success_envelope(payload, expected):
    car_id = "507f1f77bcf86cd799439011"

    def handler(request):
        assert request.url.path.endswith("/checkIsCarAvailable")
        assert request.headers["Accept"] == "application/json"
        assert request.headers["Language"] == "en"
        return httpx.Response(200, json=payload)

    filters = SearchFilters(from_date="2026-10-08", to_date="2026-10-10",
                            from_time="10:00", to_time="18:00")
    client = GearClient(settings(), httpx.MockTransport(handler))
    assert asyncio.run(client.check_availability(car_id, filters, "customer-token")) is expected


def test_protected_gear_method_requires_a_user_token():
    client = GearClient(settings(), httpx.MockTransport(
        lambda request: httpx.Response(401)))
    with pytest.raises(GearAuthorizationRequired):
        asyncio.run(client.car_details("507f1f77bcf86cd799439011", None))


@pytest.mark.parametrize("status_code", [401, 403])
@pytest.mark.parametrize("method", ["filter_catalog", "default_locations"])
def test_catalog_and_location_auth_rejections_remain_auth_errors(status_code, method):
    client = GearClient(settings(), httpx.MockTransport(
        lambda request: httpx.Response(status_code)))
    with pytest.raises(GearAuthorizationRequired):
        asyncio.run(getattr(client, method)("expired-token"))


def test_public_listing_auth_failure_is_not_mistaken_for_protected_sign_in():
    client = GearClient(settings(), httpx.MockTransport(
        lambda request: httpx.Response(401)))
    with pytest.raises(GearUnavailable):
        asyncio.run(client.search_cars(SearchFilters(location="Dubai"), None))


def test_authenticated_availability_search_forwards_only_documented_filters():
    def handler(request):
        assert request.url.path.endswith("/getAvailableCars")
        assert request.headers["language"] == "en"
        if "max_price" in request.url.params:
            assert request.url.params["min_price"] in {"0", "100.0"}
        return httpx.Response(200, json={"data": []})

    client = GearClient(settings(), httpx.MockTransport(handler))
    filters = SearchFilters(from_date="2026-10-10", to_date="2026-10-12",
                            min_price=100, max_price=500)
    assert asyncio.run(client.search_available_cars(filters, "customer-token")) == []
    assert asyncio.run(client.search_available_cars(
        SearchFilters(from_date="2026-10-10", to_date="2026-10-12",
                      location="Dubai"), "customer-token")) == []
    assert asyncio.run(client.search_available_cars(
        SearchFilters(from_date="2026-10-10", to_date="2026-10-12",
                      min_rating=4), "customer-token")) == []


def test_available_search_forwards_documented_type_and_brand_ids():
    type_id = "507f1f77bcf86cd799439012"
    brand_id = "507f1f77bcf86cd799439013"

    def handler(request):
        assert request.url.params["car_type_id"] == type_id
        assert request.url.params["brand_id"] == brand_id
        return httpx.Response(200, json={"data": []})

    filters = SearchFilters(from_date="2026-10-10", to_date="2026-10-12",
                            car_type_id=type_id, brand_id=brand_id)
    client = GearClient(settings(), httpx.MockTransport(handler))
    assert asyncio.run(client.search_available_cars(filters, "customer-token")) == []


def test_available_search_reads_later_pages_before_local_area_filtering():
    pages = []

    def handler(request):
        page = int(request.url.params["page"])
        pages.append(page)
        if page == 1:
            cars = [{"_id": f"{index:024x}", "location": "Abu Dhabi"}
                    for index in range(1, 51)]
        else:
            cars = [{"_id": f"{51:024x}", "location": "Dubai Marina"}]
        return httpx.Response(200, json={"data": {"cars": cars}})

    client = GearClient(settings(), httpx.MockTransport(handler))
    filters = SearchFilters(from_date="2026-10-20", to_date="2026-10-22",
                            location="Dubai Marina")
    cars = asyncio.run(client.search_available_cars(filters, "customer-token"))
    assert pages == [1, 2]
    assert len(cars) == 51
    assert cars[-1].location == "Dubai Marina"


def test_default_locations_forwards_user_credential_and_language_header():
    def handler(request):
        assert request.url.path.endswith("/getDefaultLocations")
        assert request.headers["Authorization"] == "Bearer customer-token"
        assert request.headers["language"] == "en"
        return httpx.Response(200, json={"data": []})

    client = GearClient(settings(), httpx.MockTransport(handler))
    assert asyncio.run(client.default_locations("customer-token")) == {"data": []}


def test_luna_extraction_uses_openai_strict_schema_and_never_receives_gear_token():
    expected = {
        "intent": "search", "selected_index": None, "reset_filters": False,
        "needs_dates": False, "needs_location": False,
        "unsupported_constraints": [], "location": "Dubai", "search": None,
        "from_date": None, "to_date": None, "from_time": None, "to_time": None,
        "min_price": None, "max_price": 500,
    }

    def handler(request):
        assert request.url.host == "api.openai.com"
        assert request.headers["Authorization"] == "Bearer test-openai-key"
        body = json.loads(request.content)
        assert body["model"] == "gpt-6-luna"
        assert body["reasoning_effort"] == "none"
        assert body["response_format"]["json_schema"]["strict"] is True
        assert "customer-token" not in request.content.decode()
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(expected),
        }}]})

    extractor = OpenAIRequirementExtractor(settings(), httpx.MockTransport(handler))
    result = asyncio.run(extractor.extract("Cars under AED 500", SearchFilters(), []))
    assert result.location == "Dubai"
    assert result.max_price == 500


def test_provider_total_timeout_is_bounded():
    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    short = replace(settings(), request_timeout=0.01)
    extractor = OpenAIRequirementExtractor(short, httpx.MockTransport(handler))
    with pytest.raises(LLMUnavailable):
        asyncio.run(extractor.extract("Cars in Dubai", SearchFilters(), []))


def test_openai_failure_logs_safe_error_category_and_status(caplog):
    def handler(request):
        return httpx.Response(429, json={
            "error": {"message": "sensitive provider detail"},
        })

    extractor = OpenAIRequirementExtractor(settings(), httpx.MockTransport(handler))
    caplog.set_level(logging.WARNING, logger="gear_ai.llm")

    with pytest.raises(LLMUnavailable):
        asyncio.run(extractor.extract(
            "private search string", SearchFilters(), [],
        ))

    assert "provider_status=429" in caplog.text
    assert "HTTPStatusError" in caplog.text
    assert "sensitive provider detail" not in caplog.text
    assert "private search string" not in caplog.text
    assert "test-openai-key" not in caplog.text


def test_openai_model_override_uses_configured_model():
    expected = {
        "intent": "booking_guidance", "selected_index": None,
        "reset_filters": False, "needs_dates": False, "needs_location": False,
        "unsupported_constraints": [], "location": None, "search": None,
        "from_date": None, "to_date": None, "from_time": None, "to_time": None,
        "min_price": None, "max_price": None,
    }

    def handler(request):
        assert request.url.host == "api.openai.com"
        assert request.headers["Authorization"] == "Bearer test-openai-key"
        body = json.loads(request.content)
        assert body["model"] == "gpt-4o-mini"
        assert "reasoning_effort" not in body
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(expected),
        }}]})

    selected = replace(settings(), openai_model="gpt-4o-mini")
    extractor = OpenAIRequirementExtractor(selected, httpx.MockTransport(handler))
    result = asyncio.run(extractor.extract("How do bookings work?", SearchFilters(), []))
    assert result.intent == "booking_guidance"
    missing_model = OpenAIRequirementExtractor(replace(selected, openai_model=""))
    with pytest.raises(ConfigurationError):
        asyncio.run(missing_model.extract("How do bookings work?", SearchFilters(), []))
    missing_key = OpenAIRequirementExtractor(replace(selected, openai_api_key=""))
    with pytest.raises(ConfigurationError):
        asyncio.run(missing_key.extract("How do bookings work?", SearchFilters(), []))


def test_model_cannot_create_a_new_tool_intent():
    bad = {
        "intent": "delete_user", "selected_index": None,
        "reset_filters": False, "needs_dates": False, "needs_location": False,
        "unsupported_constraints": [], "location": None, "search": None,
        "from_date": None, "to_date": None, "from_time": None, "to_time": None,
        "min_price": None, "max_price": None,
    }

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(bad),
        }}]})

    extractor = OpenAIRequirementExtractor(settings(), httpx.MockTransport(handler))
    with pytest.raises(LLMUnavailable):
        asyncio.run(extractor.extract("delete a user", SearchFilters(), []))
