"""SSE chat keeps the JSON contract and reports turn conflicts as events."""

import asyncio
import json

import httpx

from app.api.v1.chat import get_chat_service
from app.main import app
from app.security.sessions import SessionStore
from app.services.conversation import ConversationService
from tests.test_chat_flow import FakeExtractor, FakeGear


def _events(body: str) -> list[tuple[str, dict]]:
    events = []
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        event = "message"
        data = ""
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
        events.append((event, json.loads(data)))
    return events


def test_stream_result_matches_chat_and_replays_the_same_turn():
    service = ConversationService(FakeExtractor([]), FakeGear(), SessionStore(1800))

    async def override_service():
        return service

    app.dependency_overrides[get_chat_service] = override_service
    payload = {"message": "hello", "turn_id": "turn_stream_match_01"}

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            preflight = await client.options(
                "/api/v1/agent/chat/stream",
                headers={
                    "Origin": "https://gearapp.ae",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type,authorization,x-ai-session-id",
                },
            )
            assert preflight.status_code == 200
            assert preflight.headers.get("access-control-allow-origin") == "https://gearapp.ae"

            chat = await client.post("/api/v1/agent/chat", json=payload)
            assert chat.status_code == 200
            streamed = await client.post("/api/v1/agent/chat/stream", json={
                **payload, "session_id": chat.json()["session_id"],
            })
            assert streamed.status_code == 200
            assert streamed.headers["content-type"].startswith("text/event-stream")
            events = _events(streamed.text)
            names = [name for name, _ in events]
            assert names == ["started", "result", "done"]
            assert events[1][1] == chat.json()

            conflict = await client.post("/api/v1/agent/chat/stream", json={
                "message": "Change dates",
                "turn_id": payload["turn_id"],
                "session_id": chat.json()["session_id"],
            })
            conflict_events = _events(conflict.text)
            assert conflict_events[0][0] == "started"
            assert conflict_events[1][0] == "error"
            assert "Turn ID" in conflict_events[1][1]["detail"]

    try:
        asyncio.run(scenario())
    finally:
        app.dependency_overrides.clear()
