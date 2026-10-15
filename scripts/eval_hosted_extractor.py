"""Opt-in live language-understanding checks; no Gear reads or chat transcripts.

Run: python3 -m scripts.eval_hosted_extractor
This makes four OpenAI API calls using OPENAI_API_KEY and OPENAI_MODEL.
"""

import asyncio
import argparse
from dataclasses import dataclass

from app.clients.llm import OpenAIRequirementExtractor
from app.config import Settings
from app.schemas.chat import SearchFilters


@dataclass(frozen=True)
class Case:
    name: str
    message: str
    expected: dict[str, object]
    context: dict[str, object]


CASES = (
    Case("combined_request", "Need an SUV in Dubai Marina under AED 500 per day",
         {"location": "Dubai Marina", "car_type_name": "SUV",
          "max_price": 500, "budget_period": "day"}, {}),
    Case("memory_question", "Did I say Dubai?",
         {"intent": "context_question", "question_field": "location",
          "location": None},
         {"mode": "guided", "pending_question": "location",
          "known_fields": {"from_date": "2026-10-20", "to_date": "2026-10-22"}}),
    Case("roman_urdu_budget", "Mera budget AED 700 per day hai",
         {"max_price": 700, "budget_period": "day"},
         {"mode": "guided", "pending_question": "budget"}),
    Case("mixed_date_and_preferences", "20 TO 22 OCT, SUV, pickup 10:00, automatic",
         {"car_type_name": "SUV", "from_time": "10:00",
          "transmission": "Automatic"},
         {"mode": "guided", "pending_question": "dates"}),
)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=[case.name for case in CASES])
    args = parser.parse_args()
    cases = tuple(case for case in CASES if not args.case or case.name == args.case)
    settings = Settings.from_environment()
    if not settings.openai_api_key:
        print("SKIP: OPENAI_API_KEY is not configured")
        return 2
    extractor = OpenAIRequirementExtractor(settings)
    failures = 0
    for case in cases:
        try:
            actual = await extractor.extract_with_context(
                case.message, SearchFilters(), [], case.context,
            )
            def matches(value: object, expected: object) -> bool:
                if isinstance(value, str) and isinstance(expected, str):
                    return value.casefold() == expected.casefold()
                return value == expected

            mismatches = [key for key, expected in case.expected.items()
                          if not matches(getattr(actual, key), expected)]
            if mismatches:
                failures += 1
                details = ", ".join(
                    f"{key}={getattr(actual, key)!r} (expected {case.expected[key]!r})"
                    for key in mismatches
                )
                print(f"FAIL {case.name}: {details}")
            else:
                print(f"PASS {case.name}")
        except Exception as exc:
            failures += 1
            print(f"ERROR {case.name}: {type(exc).__name__}")
    print(f"{len(cases) - failures}/{len(cases)} hosted extraction checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
