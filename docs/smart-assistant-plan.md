# Smart rental assistant: research and implementation plan

Research date: 15 October 2026. Status: implemented with regression tests; live hosted-model and production Gear evaluations remain deployment checks.

## 1. What the reported conversation proves

The earlier date/budget patch addressed extraction failures, but the application still has no consistent policy for completing a rental request. Fixing individual phrases does not establish that policy.

I reproduced the supplied transcript in memory using the existing `FakeExtractor` and `FakeGear`, with the calendar fixed at 15 October 2026. The extractor was scripted; no external model or live inventory was queried.

| Turn | Observed application behavior | Missing behavior |
| --- | --- | --- |
| `Find a car` | Asks only for dates | Establish the search area or an explicit all-areas scope |
| `20 TO 22 OCT` | Makes one inventory call with `location=None`, `max_price=None` | Save the dates and request the missing area |
| Empty inventory response | Offers Increase budget, Nearby areas, Different vehicle type, Change dates | Choose suggestions from actual filters and supported actions |
| `WHAT AREA DID I SAID ?` | Falls through to generic rental guidance | Answer from conversation state and resume the pending question |

Code evidence:

- `app/services/conversation.py`, `chat()`: location collection depends on the model's `needs_location` flag. Having any query parameter, including dates alone, can lead to inventory search. A previous pending date question is not followed by a general completeness check.
- `app/clients/llm.py`, `Extraction` and the extraction payload: the supported intents omit questions about the current conversation. The model receives current filters and result titles, but no pending question, previous assistant action, or last search outcome. The prompt specifically requests location for “near me,” rather than defining the guided search requirements.
- `app/security/sessions.py`, `Session`: filters and several independent pending flags exist, but there is no unified task state, field provenance, or last-search explanation. A missing field cannot be distinguished from a deliberately skipped field.
- `app/services/messaging.py`, `no_results_suggestions()`: suggestions are a fixed list, irrespective of whether a budget, location, or type was supplied.
- `Extraction.filter_updates()`: `None` means no update, so removing an existing criterion needs an explicit clear operation.
- `app/services/faqs.py`, `faq_answer()`: broad keyword rules run before task interpretation. For example, cancelling a pending budget change can be mistaken for a question about booking cancellation.
- `app/services/messaging.py`, `dates_prompt()`: the fixed October date example can itself become a past-date suggestion. Generate examples from Gear's local calendar.

The transcript does not establish why live inventory was empty. The assistant must not claim that budget, location, or vehicle type caused an empty response without evidence.

## 2. Research findings and application to Gear

These sources inform the design; they do not require migrating the existing Python application to another framework.

| Primary source | Finding | Application |
| --- | --- | --- |
| [Google Cloud: parameters and form filling](https://docs.cloud.google.com/dialogflow/cx/docs/concept/parameter) | Required information has explicit collection rules, session values, and reprompt behavior. | Put task requirements in the application controller rather than relying on model flags. |
| [Google: confirmations and corrections](https://developers.google.com/assistant/conversation-design/confirmations) | Briefly acknowledge understood parameters and allow direct corrections. | “20–22 October, got it. Which area?” Accept “Actually Sharjah” without restarting. |
| [Rasa: conversation repair patterns](https://rasa.com/docs/reference/primitives/patterns/) | Interruptions, corrections, cancellations, skipped questions, clarification, and errors need explicit handling. | A question about the search temporarily interrupts collection; answering it preserves the pending step. |
| [SIXT: reservation steps](https://www.sixt.com/help-center/articles/how-do-i-book-a-sixt-rental-car-in-three-steps/) | Its reservation search starts with locations, dates, and times before vehicle selection. | Location and rental period are the foundation of a guided rental search. This is domain evidence, not a statement about Gear's API requirements. |

Recommended Gear policy: distinguish guided rental search from browsing listings. Accept information in any order, select the next missing requirement consistently, and let users explicitly skip optional preferences.

## 3. Conversation contract

### Default guided sequence

1. **Area:** ask for city/area, accept a location shared by the app, or record an explicit “anywhere” choice. Do not infer an area from nothing.
2. **Rental dates:** collect valid pickup/return dates; ask only for the missing part. Preserve dates supplied before the area. Generate future examples using Gear's local calendar.
3. **Budget:** offer one short, skippable question with “Show options without a limit.” Distinguish a daily budget from a total trip budget; do not silently treat one as the other.
4. **Preferences:** use vehicle type, seating, transmission, and trip needs already provided. Ask another preference question only when necessary to resolve the request; a vehicle type is not mandatory.
5. **Search:** summarize the actual scope and filters in the search response. Follow the existing Gear authentication requirements for date-based availability.
6. **Refine or inspect:** handle corrections, filter removal, option references, availability checks, and booking guidance within the current task.

An explicit “show cars now” can bypass optional questions once the essential scope and dates are known. “Just browsing” uses listing mode and does not require rental dates or budget. Browsing still establishes a place or explicit all-areas scope. Exact pickup/return times are collected when the relevant availability operation needs them, following the verified Gear contract.

### Correct handling of the reported sequence

> User: Find a car  
> Assistant: Which city or area would you like to rent in?  
> User: 20 TO 22 OCT  
> Assistant: 20–22 October, got it. Which city or area should I search?  
> User: WHAT AREA DID I SAID?  
> Assistant: You haven't specified an area yet. I have your dates as 20–22 October. Which city or area would you like?  
> User: Dubai Marina  
> Assistant: Dubai Marina for 20–22 October. Do you have a daily budget, or should I show options without a limit?

If an area was previously supplied, the assistant names it. If location came from GPS, it says it is using the shared location instead of claiming the user named that area.

### Rules that apply at every step

- Accept multiple details in one message and do not ask again for validated details.
- Answer “What area/dates/budget did I give?”, “What are you searching for?”, and “Why are you asking?” from stored state.
- Treat questions as questions: “Did I say Dubai?” must not set the area to Dubai.
- A question or FAQ does not overwrite filters or abandon the pending task. Resolve “cancel this change” in task context before routing to booking-policy FAQs.
- A correction changes only the intended fields. “Remove the budget” clears it; “no preference” records a deliberate skip.
- Never search while an essential field is invalid or missing. Never reinterpret an unclear bare number as an unrelated field.
- Keep unknown inventory, service failures, missing verified attributes, and successful empty searches distinct.

## 4. Implementation architecture

Keep FastAPI, Gear clients, validated cards, and the existing authentication boundary. Introduce a clear division between understanding a message, managing state, and deciding what action is allowed.

```text
Message + structured conversation context
  -> Understand intent and proposed changes
  -> Validate and apply explicit set/clear/skip operations
  -> Answer a context question or handle an interruption
  -> Compute the next missing requirement from task policy
  -> Ask one question, or execute the permitted Gear read
  -> Render a grounded reply and applicable suggestions
```

### A. Structured conversation state

Extend `Session` with an active task/mode, typed pending question, per-field status and source, last assistant action, last search snapshot/outcome, and a criteria revision. Distinguish `missing`, `provided`, `skipped`, and `invalid` where applicable. Record whether a location was typed, shared, or explicitly unrestricted.

Keep the user's requested criteria separate from what an endpoint actually applied. Bind result references to their search revision so changing a search cannot silently reuse an old option number. Preserve the last successful result set if a later search fails, while clearly identifying it as previous results.

### B. Understanding messages

Expand extraction beyond search parameters to semantic actions: provide/correct/clear criteria, ask about criteria, explain the last action, change a criterion, skip a question, cancel a pending change, restart, and existing vehicle/FAQ actions.

Supply a compact context object containing the current task, known fields with their sources, pending question, last assistant action, last tool outcome, bounded recent turns, and valid result references. Exclude credentials. Validate proposed operations before changing state. The server assigns provenance; the model does not invent it.

Use the model for flexible language and combined requests. Keep local handling for explicit button actions, known context questions, and unambiguous date/amount formats. A fallback must preserve additional information or ask for clarification; it must not silently discard times, preferences, or corrections from a richer message.

### C. Task policy and search gate

Add `app/services/dialogue_policy.py` for task requirements, next-question selection, search readiness, and permitted refinements. `ConversationService` coordinates those decisions rather than accumulating more independent pending flags and phrase-specific branches.

Guided search readiness depends on validated area/scope and dates. Optional-question status controls whether to ask about budget or proceed. Listing browsing and selected-car availability have their own requirements. Model `needs_*` hints do not override application requirements.

### D. Replies and suggestions

Change suggestion generation to consume filters, outcome, pending question, and supported capabilities:

- Budget exists: “Increase budget” asks for a new maximum if absent. No budget exists: use “Set budget,” where useful.
- Area exists and a supported expansion is available: offer “Nearby areas.” Otherwise ask for an area or omit the action. Do not invent neighboring locations.
- A type filter exists: offer changing/removing it. Otherwise offer choosing a type when supported.
- Dates exist: “Change dates” asks for the new range and preserves the other criteria.
- A service error: offer retry with the saved request, not irrelevant no-match refinements.

Every visible suggestion needs a defined handler, prerequisites, and expected state transition. Keep existing string suggestions compatible initially; add optional stable action identifiers when coordinating Web/Mobile changes.

### E. Gear behavior and session continuity

Audit requested versus applied filters and pagination. Currently `getAvailableCars` does not forward the location string; the application filters returned cards locally, after fetching a limited page. An empty locally filtered page is insufficient evidence that no matching car exists anywhere in the full inventory. Verify backend location semantics or process pagination before making exhaustive claims.

Retain pending work across sign-in and revalidate it before resuming. Replace the in-memory session implementation with a deployment-compatible shared store before production/multiple-worker rollout, retaining the separate conversation-token and Gear-token boundary. Detect session expiry and explain it instead of appearing to remember missing context.

## 5. Delivery order and reviewable outputs

| Phase | Work | Completion evidence |
| --- | --- | --- |
| 1. Contract and baseline | Capture reported transcripts; document guided/browse requirements; fix test clocks that depend on now-past fixture dates. | Deterministic baseline and explicit behavior examples in the repository. |
| 2. State and policy | Add typed state, field operations/provenance, context-question handlers, and the search gate. | Dates-first input saves dates, asks for area, and makes zero inventory calls. |
| 3. Understanding and repair | Expand extraction/context; integrate interruptions, corrections, skips, and explicit clears with local fallbacks. | Questions during budget/date collection are answered and the task resumes correctly. |
| 4. Grounded results and UI actions | Generate applicable suggestions; implement every offered action; verify filters, empty-result semantics, and pagination. | No increase-budget action without an existing budget; no invented nearby areas or unsupported causal explanations. |
| 5. End-to-end verification and readiness | Exercise browser/API flows, live language-understanding evaluations, outage/auth recovery, and session continuity. Implement shared sessions for the deployment target. | Critical journeys pass across Web/Mobile contracts; safe traces explain each decision. |

Use the existing `app/`, `tests/`, and `docs/` layout. Implement in coherent phases with regression coverage rather than rewriting the Gear integration at once.

## 6. Acceptance scenarios

Assert the reply, state before/after, next pending question, suggestions, and external call count for each conversation—not just whether a message contains a keyword.

| Scenario | Required outcome |
| --- | --- |
| Bare `Find a car` | Requests area; zero inventory calls. |
| Dates supplied when area was requested | Stores dates, still requests area; zero inventory calls. |
| All details supplied together | Extracts all, skips answered questions, searches the actual request. |
| `What area did I say?` with no area | Says none was supplied, mentions known dates where useful; no mutation/search. |
| Same question with an existing area | Answers the stored area and its source accurately. |
| GPS supplied or denied | Uses supplied coordinates or requests a typed area; no assumed city. |
| `Anywhere` or `Just browsing` | Records explicit scope/mode and applies that mode's requirements. |
| `Increase budget` without a value | Asks for the new maximum; no repeated search. |
| Amount-only response to budget prompt | Updates the intended budget, preserves other criteria, handles its period. |
| Budget or area question while another field is pending | Answers it, preserves the pending question; no loop or mutation. |
| `Remove budget` / `Any vehicle type` | Clears that restriction and preserves the rest. |
| Daily versus total budget unclear | Clarifies the period rather than silently applying the wrong limit. |
| `Nearby areas` without a starting area | Requests the starting area; no fabricated expansion/search. |
| `Actually Sharjah` | Replaces location only; invalidates stale search references appropriately. |
| `Change dates` without new dates | Asks for new dates; keeps area, budget, and preferences. |
| Invalid/past/reversed or ambiguous dates | Requests the necessary correction; zero inventory calls. |
| `Cancel that budget change` versus cancellation-policy question | Cancels the pending change or answers the policy, according to meaning/context. |
| Successful empty search without budget/type | Does not suggest increasing a nonexistent budget or imply a nonexistent type restriction. |
| Timeout/provider outage/API rejection | Preserves context; returns an actionable service/auth response, never a no-match claim. |
| Date reply also contains times and preferences | Preserves all supplied information or clarifies what could not be understood. |
| Sign-in and retry | Resumes the current operation with validated criteria and correct selected car. |
| Old option number after a new search | Refers to the current result set or asks which car; never silently selects an old car. |
| Repeated button click or duplicate request | Does not advance two steps or apply a change twice. |
| Restart, expiry, and separate users | Clear or restore state according to the session contract; no cross-user context. |
| Every displayed suggestion | Has a functioning handler and valid prerequisites in that exact state. |

Add policy/unit and API tests, generated input-order/interrupt sequences, and a separate hosted-extractor evaluation for paraphrases, typos, uppercase messages, and mixed English/Roman Urdu phrasing. Scripted extraction tests alone cannot establish natural-language understanding quality. Freeze the calendar in date-sensitive tests; retain dedicated timezone/year-rollover cases.

Release criteria: all critical reported journeys and search-gate invariants pass; existing relevant tests pass with stable clocks; visible actions work; external failures preserve the task; and traces record the chosen action, pending requirement, criteria revision, and safe error category without credentials or raw private chat text.

## 7. Implementation and verification record

Implemented in `app/services/dialogue_policy.py`, `app/services/conversation.py`,
`app/security/sessions.py`, `app/security/shared_sessions.py`, the Gear client,
and the browser playground. The guided flow collects area, dates, and an optional
daily budget in any order; context questions and corrections preserve the task.
Search results and suggestions are grounded in saved criteria. A selected vehicle
can continue through date/time and sign-in questions, while a separate scope
change invalidates stale result numbers. The client reads up to ten 50-car pages
before local-only filters; reaching that bound reports an unavailable search,
not a false empty result. Session persistence and a per-turn distributed lease
are enabled by `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` and are
mandatory for `APP_ENV=production`.

Verification on 15 October 2026: `python3 -m pytest -q` passed 163 tests;
`python3 -m scripts.eval_hosted_extractor` passed 4/4 live language-understanding
checks using the configured hosted extractor. A localhost demo API and browser
smoke test reproduced the reported Find a car → dates → “WHAT AREA DID I SAID?”
journey with the expected saved-state reply. The shared-store REST contract and
cross-worker behavior are covered with a mocked transport; a real Upstash
instance and authenticated live Gear availability were not available for this
local verification, so those are required deployment smoke checks.
