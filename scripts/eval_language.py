"""Fixed prompts for hosted language quality. Skips when no API key is configured.

Run: python3 -m scripts.eval_language
"""

import asyncio

from app.clients.llm import OpenAIRequirementExtractor
from app.config import Settings
from app.schemas.chat import ChatRequest, SearchFilters
from app.security.sessions import SessionStore
from app.services.conversation import ConversationService
from app.services.dialogue_policy import next_question


PROMPTS = (
    ("typo_area", "wat area did i give"),
    ("short", "suv dubai"),
    ("roman_urdu", "dubai mein car chahiye"),
    ("cancelation", "cancelation policy"),
    ("why_dates", "why dates"),
)


async def main() -> int:
    settings = Settings.from_environment()
    if not settings.openai_api_key:
        print("SKIP: OPENAI_API_KEY is not configured")
        return 2
    extractor = OpenAIRequirementExtractor(settings)
    chat = ConversationService(extractor, _EmptyGear(), SessionStore(1800))
    opened = await chat.chat(ChatRequest(message="Cars in Dubai"), None)
    print(f"opening status={opened.status} location={opened.applied_filters.location}")
    failures = 0
    for name, message in PROMPTS:
        extracted = await extractor.extract_with_context(
            message, SearchFilters(location="Dubai"), [], {"pending_question": "dates"},
        )
        question = next_question(
            "browse", SearchFilters(location="Dubai"), anywhere=False,
            budget_decided=True, location_locked=True,
        )
        turn = await chat.chat(ChatRequest(message=message, session_id=opened.session_id), None)
        asked_location_again = (
            "which city" in turn.message.lower() and turn.status == "clarification"
        )
        print(
            f"{name} intent={extracted.intent} location={extracted.location} "
            f"next={question} status={turn.status} asked_location_again={asked_location_again}"
        )
        if asked_location_again:
            failures += 1
    return 1 if failures else 0


class _EmptyGear:
    async def search_cars(self, filters, token):
        return []

    async def search_available_cars(self, filters, token):
        return []

    async def filter_catalog(self, token):
        return {"data": {"car_types": [], "brands": []}}

    async def car_details(self, car_id, token):
        raise RuntimeError("unused")

    async def check_availability(self, car_id, filters, token):
        return False


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
