# Gear AI Agent — App developer handover

This document is the integration contract for **Gear Web** and **Gear Mobile**. The service is a stateless HTTP API (FastAPI) that turns natural-language messages into Gear search, details, and availability flows. Gear remains the source of truth for inventory and authentication.

## Deployed base URL

After Vercel deploy, set your client base URL to the project domain, for example:

`https://<your-vercel-project>.vercel.app`

All paths below are relative to that host.

| Resource | Path |
|----------|------|
| Health | `GET /health` |
| OpenAPI (Swagger UI) | `GET /docs` |
| OpenAPI JSON | `GET /openapi.json` |
| Agent metadata | `GET /api/v1/agent/meta` |
| Chat | `POST /api/v1/agent/chat` |
| Reset conversation | `POST /api/v1/agent/session/reset` |
| Browser playground (non-production only) | `GET /chat` or `GET /playground` |

Every response includes `X-Request-ID` for support correlation.

---

## Authentication model (two separate tokens)

| Token | Who issues it | Purpose |
|-------|----------------|---------|
| **AI session** | This service | Multi-turn conversation state (filters, last results, pending login retry) |
| **Gear bearer** | Gear login | Protected Gear read APIs (details, catalog, date availability) |

The Gear bearer is **never** stored server-side. Send it on each chat request that needs protected data. The AI session is independent of Gear login: reuse the same `session_id` before and after the user signs in.

When Gear auth is required, the API returns `status: "authentication_required"` and `action: { "type": "SIGN_IN" }`. After login, retry with the **same AI session** and a valid `Authorization: Bearer <gear_token>` header.

---

## Session transport: Web vs Mobile

### Mobile (recommended: header / body)

1. First message: omit `session_id`.
2. Read `session_id` from the JSON response and persist it (secure storage).
3. Send it on every follow-up via `X-AI-Session-ID` **or** `session_id` in the JSON body (not both with different values).

```http
POST /api/v1/agent/chat
Content-Type: application/json
X-AI-Session-ID: <token-from-prior-response>

{"message":"Show SUVs in Dubai under AED 500","locale":"en","context":{}}
```

### Web (same-origin cookie mode)

1. First request: send `X-AI-Session-Transport: cookie`.
2. The service sets `HttpOnly` cookie `ai_session_id` on path `/api/v1/agent`.
3. Responses omit `session_id` from JSON in cookie mode; the browser sends the cookie automatically on later `/api/v1/agent/*` calls.

```http
POST /api/v1/agent/chat
Content-Type: application/json
X-AI-Session-Transport: cookie

{"message":"Cars near me","context":{"lat":25.08,"lng":55.14}}
```

Reset:

```http
POST /api/v1/agent/session/reset
X-AI-Session-ID: <token>   # mobile
# or rely on ai_session_id cookie on web
```

**Conflict rule:** If `session_id` in the body, `X-AI-Session-ID`, and `ai_session_id` cookie disagree, the API returns `400` with `Conflicting AI session IDs`.

---

## `POST /api/v1/agent/chat`

### Request body

Defined in `app/schemas/chat.py` (`ChatRequest`).

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `message` | string | yes | 1–2000 chars, trimmed |
| `session_id` | string | no | Mobile; pattern `^[A-Za-z0-9_-]{22,128}$` |
| `locale` | `"en"` | no | Default `en` |
| `context` | object | no | See below |

**`context` (optional client hints)**

| Field | Type | Notes |
|-------|------|-------|
| `lat`, `lng` | number | Both required together; used for “near me” |
| `car_type_id` | Mongo ObjectId string | Single type filter |
| `car_type_ids` | array of ObjectIds | Do not combine with `car_type_id` |
| `brand_id` | ObjectId | Brand filter |

### Headers

| Header | When |
|--------|------|
| `Authorization: Bearer <gear_jwt>` | Logged-in flows (details, availability, catalog-backed filters) |
| `X-AI-Session-ID` | Mobile session |
| `X-AI-Session-Transport: cookie` | Web cookie session |

### Response body (`ChatResponse`)

| Field | Description |
|-------|-------------|
| `session_id` | Present in mobile/header mode; omitted when using cookie transport |
| `status` | See status table below |
| `message` | User-facing assistant text |
| `vehicles` | `VehicleCard[]` when listing or detailing |
| `suggestions` | Optional quick-reply strings |
| `applied_filters` | Normalized `SearchFilters` after this turn |
| `available` | Boolean when `status` is `availability` |
| `data_source` | `"gear"` or `"demo"` |
| `action` | `{ "type": "SIGN_IN" }` when login is required |

**`status` values**

| Status | Meaning |
|--------|---------|
| `clarification` | Need more info (dates, location, etc.) |
| `results` | Search results in `vehicles` |
| `details` | Single vehicle detail |
| `availability` | Availability answer for selected car |
| `guidance` | Policy / FAQ style answer |
| `unavailable` | Could not fulfill (upstream or guardrail) |
| `authentication_required` | Send Gear bearer and retry same session |

**`VehicleCard`** (summary): `id`, `title`, `location`, `price` (daily basis), `rating`, type/brand names, seats, fuel, transmission, `match_reason`, etc. Prices are **daily rates**, not checkout totals.

---

## `GET /api/v1/agent/meta`

Runtime flags for the client UI (no secrets):

```json
{
  "mode": "demo",
  "extractor": "hosted",
  "llm_provider": "openai",
  "gear_configured": true,
  "customer_token_needed_for_details": false,
  "protected_features_require_login": false
}
```

In **live** mode, `customer_token_needed_for_details` and `protected_features_require_login` are `true`.

---

## CORS (browser clients)

If Web calls this API from a different origin, set server env:

```bash
CORS_ALLOW_ORIGINS=https://your-web-app.com,http://localhost:3000
```

Comma-separated list; credentials are allowed so cookie mode can work when origins are explicit.

---

## Example flows

### Guest search (public Gear listings)

```bash
curl -sS -X POST "$BASE/api/v1/agent/chat" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Show cars in Dubai under AED 500"}'
```

### Multi-turn with session (mobile)

```bash
SESSION=$(curl -sS -X POST "$BASE/api/v1/agent/chat" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Family car this weekend"}' | jq -r .session_id)

curl -sS -X POST "$BASE/api/v1/agent/chat" \
  -H 'Content-Type: application/json' \
  -H "X-AI-Session-ID: $SESSION" \
  -d '{"message":"Dubai Marina from 2026-10-10 to 2026-10-12"}'
```

### Protected detail after login

```bash
curl -sS -X POST "$BASE/api/v1/agent/chat" \
  -H 'Content-Type: application/json' \
  -H "X-AI-Session-ID: $SESSION" \
  -H "Authorization: Bearer $GEAR_TOKEN" \
  -d '{"message":"View option 1"}'
```

---

## Vercel environment variables

Set these in the Vercel project (**Settings → Environment Variables**). Do not commit secrets.

### Recommended for first integration (staging + demo inventory)

Safe for app devs without production Gear credentials:

| Variable | Example | Notes |
|----------|---------|-------|
| `APP_ENV` | `staging` | Enables `/chat` playground; allows `DEMO_MODE` |
| `DEBUG` | `false` | Required for staging/production |
| `DEMO_MODE` | `true` | Sample vehicles; `data_source: "demo"` |
| `DEMO_EXTRACTOR` | `hosted` | Uses OpenAI on server |
| `OPENAI_API_KEY` | *(secret)* | Server only |
| `OPENAI_MODEL` | `gpt-6-luna` | Or another supported model |
| `CORS_ALLOW_ORIGINS` | your web origins | For cross-origin browser calls |
| `SESSION_TTL_SECONDS` | `1800` | AI session lifetime |
| `UPSTASH_REDIS_REST_URL` | *(HTTPS URL)* | Shared AI sessions; required in production |
| `UPSTASH_REDIS_REST_TOKEN` | *(secret)* | Writable REST token; server only |
| `REQUEST_TIMEOUT` | `15` | Upstream HTTP timeout |

### Live Gear (pre-production / production)

| Variable | Example |
|----------|---------|
| `APP_ENV` | `production` (or `staging` for playground + live data) |
| `DEMO_MODE` | `false` |
| `GEAR_API_BASE_URL` | `https://gearapp.ae:5001` |
| `GEAR_API_PREFIX` | `/Gear/api` |
| `OPENAI_API_KEY` | *(secret)* |

`DEMO_MODE=true` is **not** allowed when `APP_ENV=production`.

---

## Deploy on Vercel

1. Connect this repository to Vercel (or run `npx vercel` from the repo root).
2. Vercel detects FastAPI at `app/main.py` (Python 3.12 via `.python-version`).
3. Add environment variables above for **Preview** and/or **Production**.
4. Deploy. Verify `GET /health` and `GET /docs`.

`vercel.json` sets `maxDuration: 60` for LLM + Gear calls.

### Serverless session limitation (important)

AI sessions are stored **in memory** in the current implementation. On Vercel, multiple instances and cold starts mean:

- A `session_id` may not survive if the next request hits another instance.
- For production scale, plan a shared session store (Redis, etc.) before high traffic.

For short demos and single-instance warm traffic, multi-turn chat usually works; treat lost sessions as “start new chat” in the client.

---

## Out of scope (current release)

- Creating or confirming bookings in Gear (assistant directs users to the app).
- Checkout / final price quotes.
- Persistent session store across serverless instances (see above).

---

## Support

- API contract source: `app/schemas/chat.py`
- Architecture: [architecture.md](./architecture.md)
- Gear filter notes: [gear-api-filter-audit.md](./gear-api-filter-audit.md)
- Include `X-Request-ID` from failing responses when reporting issues.
