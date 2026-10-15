"""Shared session state survives worker changes and serializes turn retries."""

import asyncio
from datetime import date
import json

import httpx

from app.clients.demo import DemoExtractor, DemoGearClient
from app.schemas.chat import ChatRequest, ChatResponse
from app.security.shared_sessions import SharedSessionStore, SessionStoreUnavailable
from app.services.conversation import ConversationService, TurnConflict


def redis_transport():
    records: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-redis-secret"
        command = json.loads(request.content)
        operation, key = command[:2]
        if operation == "GET":
            result = records.get(key)
        elif operation == "SET":
            if "NX" in command and key in records:
                result = None
            else:
                records[key] = command[2]
                result = "OK"
        elif operation == "DEL":
            result = int(records.pop(key, None) is not None)
        elif operation == "EVAL":
            lock_key, lease = command[3:]
            result = int(records.get(lock_key) == lease)
            if result:
                del records[lock_key]
        else:
            raise AssertionError(command)
        return httpx.Response(200, json={"result": result})

    return httpx.MockTransport(handle), records


def test_shared_store_round_trips_state_without_leaking_token_in_key():
    transport, records = redis_transport()
    first = SharedSessionStore(1800, "https://redis.example", "test-redis-secret", transport)
    second = SharedSessionStore(1800, "https://redis.example", "test-redis-secret", transport)
    session = first.get_or_create(None)
    session.filters.location = "Dubai Marina"
    session.filters.from_date = date(2026, 10, 20)
    session.pending_requirement = "dates"
    session.recent_responses["a" * 16] = (
        "What area?", ChatResponse(session_id=session.public_token,
                                   status="guidance", message="Dubai Marina."),
    )
    first.save(session)
    recovered = second.get_or_create(session.public_token)
    assert recovered.filters.location == "Dubai Marina"
    assert recovered.filters.from_date == date(2026, 10, 20)
    assert recovered.pending_requirement == "dates"
    assert recovered.recent_responses["a" * 16][1].message == "Dubai Marina."
    assert all(session.public_token not in key for key in records)
    second.delete(session.public_token)
    assert second.get_or_create(session.public_token).id != session.id


def test_two_workers_preserve_guided_question_and_idempotent_turn():
    transport, _ = redis_transport()
    first = ConversationService(
        DemoExtractor(), DemoGearClient(),
        SharedSessionStore(1800, "https://redis.example", "test-redis-secret", transport),
        data_source="demo", today=lambda: date(2026, 10, 15),
    )
    second = ConversationService(
        DemoExtractor(), DemoGearClient(),
        SharedSessionStore(1800, "https://redis.example", "test-redis-secret", transport),
        data_source="demo", today=lambda: date(2026, 10, 15),
    )

    async def scenario():
        opening = await first.chat(ChatRequest(message="Find a car", turn_id="a" * 16), None)
        dates = ChatRequest(message="20 to 22 October", session_id=opening.session_id,
                            turn_id="b" * 16)
        answer = await second.chat(dates, None)
        repeated = await first.chat(dates, None)
        assert repeated == answer
        assert "area" in answer.message.lower()
        question = await first.chat(ChatRequest(message="WHAT AREA DID I SAY?",
                                                session_id=opening.session_id), None)
        assert "haven't specified" in question.message.lower()
        try:
            await second.chat(ChatRequest(message="Different message",
                                          session_id=opening.session_id,
                                          turn_id="b" * 16), None)
        except TurnConflict:
            pass
        else:
            raise AssertionError("Reusing a turn ID with different text must fail")

    asyncio.run(scenario())


def test_shared_store_rejects_bad_endpoint_and_store_errors():
    try:
        SharedSessionStore(1800, "http://redis.example", "secret")
    except ValueError:
        pass
    else:
        raise AssertionError("Redis REST must use HTTPS")

    failing = httpx.MockTransport(lambda request: httpx.Response(503))
    store = SharedSessionStore(1800, "https://redis.example", "secret", failing)
    try:
        store.get_or_create(None)
    except SessionStoreUnavailable:
        pass
    else:
        raise AssertionError("Shared-store failure must not silently start a new session")
