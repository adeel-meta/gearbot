# Gear AI Agent

FastAPI service for conversational Gear vehicle search across Web and Mobile.

## Requirements

- Python 3.12+ locally (Vercel uses 3.12 from `.python-version`)

## Setup

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On Windows, activate the environment with `.venv\Scripts\activate`.
For a fresh checkout, copy `.env.example` to `.env` and add local secrets there. Preserve an existing `.env` when setting up again.

## App developer handover

Integration guide for Web/Mobile teams (endpoints, sessions, auth, Vercel env): [docs/APP_DEV_HANDOVER.md](docs/APP_DEV_HANDOVER.md).

## Environment files and Vercel

**`.env` does not go to GitHub** (secrets stay local). Vercel does not read `.env` from the repo; add the same keys under **Project → Settings → Environment Variables** (use **Import .env** and paste your local file). Step-by-step: [docs/VERCEL_ENV.md](docs/VERCEL_ENV.md).

## Deploy on Vercel

1. Import the repo in Vercel or run `npx vercel` from the project root.
2. Python entrypoint is `app/main.py` (see `vercel.json` for function timeout).
3. For a safe shared preview for app devs, use **staging** + demo inventory:

   | Variable | Value |
   |----------|-------|
   | `APP_ENV` | `staging` |
   | `DEBUG` | `false` |
   | `DEMO_MODE` | `true` |
   | `DEMO_EXTRACTOR` | `hosted` |
   | `OPENAI_API_KEY` | *(Vercel secret)* |
   | `CORS_ALLOW_ORIGINS` | your web app origins |

4. Open `https://<deployment>/docs` for Swagger and `https://<deployment>/chat` for the playground.

Live Gear preview: set `DEMO_MODE=false`, add `GEAR_API_BASE_URL` and `GEAR_API_PREFIX`, keep `APP_ENV=staging` if you still want `/chat`.

## Run locally

```bash
python -m app
```

This reads `HOST` and `PORT` from `.env`; the provided development values bind to `0.0.0.0:8000`. Open `http://127.0.0.1:8000/chat` to chat, `/docs` for Swagger UI, or `/health` for the health check.

## Interactive playground

The local `.env` uses live mode and the OpenAI provider. `GEAR_API_BASE_URL=https://gearapp.ae:5001` and `GEAR_API_PREFIX=/Gear/api` point to the public listing API used by Gear's web app. The `getCars` route was verified to return JSON that the vehicle mapper accepts. This app reads Gear inventory through that API; it has no direct database connection.

For an offline sample conversation, explicitly set `DEMO_MODE=true` and `DEMO_EXTRACTOR=local`, then restart. Every vehicle, price, rating, and availability result is sample data, marked in the interface and API with `data_source: "demo"`. If a local web port is unavailable, use `python -m app.demo_cli`. Example demo sequence:

1. `I need a family car for the weekend`
2. `Dubai Marina`
3. Give future dates such as `2026-10-20 to 2026-10-22`
4. `Under AED 500`, then `View option 1`

To exercise your configured OpenAI API key while keeping sample vehicles, set `DEMO_MODE=true` and `DEMO_EXTRACTOR=hosted`. The provided environment files set `OPENAI_MODEL` to [`gpt-6-luna`](https://developers.openai.com/api/docs/models/gpt-6-luna), which supports structured output with Chat Completions. Restart the server. The browser never receives the API key. If OpenAI is unreachable, chat returns a controlled unavailable response.

For live Gear inventory, keep `DEMO_MODE=false` and use the confirmed Gear host. Guests can chat and search listings through public `getCars`. Full details, catalog data, locations, date-based availability search, and availability checks require a logged-in user's Gear bearer token. In the local `/chat` playground, paste a fresh token into the masked sidebar field to test those protected checks; the field accepts either the raw token or a `Bearer ` prefix. It stays only in page memory and clears on refresh or with **Clear token**. The AI service forwards that token only to protected read endpoints; Gear validates it. Without a token, or if Gear returns 401/403, the assistant returns `authentication_required` with `SIGN_IN` and keeps the pending protected operation. Send the same AI session with a valid Bearer token to retry it automatically. Relative day ranges such as `5 to 8 of this month` are resolved in the Gear (Dubai) timezone, and past pickup dates are rejected before calling Gear. No shared Gear service credential is used.

To check the public route from your own machine before chatting, run `python -m app.check_gear`. It reports HTTP status and JSON shape without printing vehicle records or credentials, and exits with an error if the route does not return usable vehicle JSON.

To inspect the authenticated type/brand catalog, run `python -m scripts.inspect_gear_catalog` in a terminal and enter a dedicated customer test token at its hidden prompt. The script prints only response keys and types; it does not store or log the token, and the chat service never uses it or sends it to OpenAI.

## Chat API

Web and Mobile can use `POST /api/v1/agent/chat` with a JSON `message`, optional `turn_id`, optional session token and optional `context` coordinates or catalog IDs (`car_type_id`, `car_type_ids`, `brand_id`). Supply a fresh 16–128 character `turn_id` for each user turn and reuse it only when retrying that same turn; a reused ID with different text returns HTTP 409. Same-origin Web sends `X-AI-Session-Transport: cookie` on its first request and then reuses the `HttpOnly` `ai_session_id` cookie; cookie-mode responses omit the token from JSON. Mobile sends `X-AI-Session-ID` or `session_id` in the body and uses the returned token in secure local storage. The same AI session token is reused before and after Gear login; it is independent of the Gear bearer. The user bearer is forwarded only to protected Gear read routes, where Gear validates it. `POST /api/v1/agent/session/reset` deletes the current conversation; Web also clears its cookie. Natural-language type and brand names are resolved against the authenticated Gear catalog; if login is required, the pending search resumes in the same conversation. The browser supplies coordinates, with permission, for “near me” prompts.
Responses include `X-Request-ID` for troubleshooting; request logs contain the route and status, not message text or credentials.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/agent/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Show cars in Dubai under AED 500"}'
```

The verified public `getCars` response uses `data.cars`; the mapper also accepts a `data` list or a `data.vehicles`, `data.results`, or `data.items` list. Local development uses in-memory sessions, which reset on restart. Set `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` to share conversations across workers; production refuses to start without them. If that store is unavailable, chat returns HTTP 503 rather than silently forgetting context. Expired sessions get a new token and a one-time explanation. Details and availability require a car selected from the current result list plus the user's Gear bearer token. Checkout totals remain disabled until the endpoint's behavior and response are confirmed. Booking requests return a `SIGN_IN` action and tell the user to complete booking in the Gear app; the agent does not create bookings.

A card's `price` uses Gear's numeric `price`, `day_price_with_fee`, or `day_price` field in that order and labels it as a daily rate, never as a checkout total. Live ratings use `avg_rating`. Verified `no_seats`, type, brand, fuel, mileage, transmission, luggage, and explicit family-suitability fields are mapped when Gear returns them. Missing attributes produce a data-unavailable response instead of a guessed recommendation.

The app uses the OpenAI API for requirement extraction. Configure `OPENAI_API_KEY` on the server; `OPENAI_MODEL` defaults to `gpt-6-luna` and can be changed to another supported OpenAI model.

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

For an opt-in hosted language check (four OpenAI API calls, no Gear reads), run
`python3 -m scripts.eval_hosted_extractor` after configuring `OPENAI_API_KEY`.
See [docs/smart-assistant-plan.md](docs/smart-assistant-plan.md) for the guided
search contract, regression scenarios, and deployment verification record.

## Read-only MongoDB schema inspection

The running chat app uses Gear's API; MongoDB access is only for the development utility in `scripts/`. Create a separate MongoDB Atlas database user with the `read` role on the Gear database, then set `GEAR_READONLY_DB_URL` and `GEAR_DB_NAME` in the untracked `.env`. The inspector refuses any credential with write or unverified permissions and never uses the older `MongoDB_URL` setting. MongoDB's built-in `read` role covers all non-system collections in one database; use an Atlas custom role for collection-specific access.

```bash
python -m pip install -r requirements-dev.txt
python -m scripts.inspect_gear_db --check-access
python -m scripts.inspect_gear_db
```

The utility checks effective privileges before reading collection metadata. It samples at most 20 documents from matching car, type, brand, location, and deal collections and prints field names, types, and a few safe categorical values. It does not print records, IDs, contact details, or the connection URL. The `scripts/` directory contains development utilities and is not imported by the running app.

## Current scope

This implementation includes natural-language search, multi-turn duration handling, authenticated type/brand catalog resolution, verified seat/fuel/transmission filtering, conservative family ranking, browser geolocation, date-based availability, selected-car checks, and FAQ answers grounded in Gear's published terms and cancellation policy. Checkout totals and persistent production sessions remain later stages. Booking execution is outside agent scope; users sign in and book in Gear.

The planned web and mobile conversation flow, Gear API boundaries, and delivery stages are in [the architecture plan](docs/architecture.md).
The [Gear API filter audit](docs/gear-api-filter-audit.md) records observed response fields, unverified filters, and backend follow-up items.
