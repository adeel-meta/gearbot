# Why `.env` is not on GitHub

The file `.env` is listed in `.gitignore` on purpose. It holds secrets (`OPENAI_API_KEY`, tokens). **Never commit `.env` to GitHub.**

What **is** on GitHub:

- `.env.example` — variable names and safe defaults (no real secrets)

What Vercel needs:

- The **same keys** as in your local `.env`, added in the Vercel project (not as a file in the repo).

---

## Option A — Import in Vercel (easiest)

1. Open [Vercel Dashboard](https://vercel.com) → your **GEARCHATBOT** project.
2. **Settings** → **Environment Variables**.
3. Click **Import .env** (or paste bulk).
4. Paste the **contents of your local `.env`** file.
5. Select **Production**, **Preview**, and **Development**.
6. Save, then **Deployments** → **Redeploy** the latest deployment.

For app-dev handover (demo + playground), prefer these on Vercel (adjust if you use live Gear):

```env
APP_ENV=staging
DEBUG=false
DEMO_MODE=true
DEMO_EXTRACTOR=hosted
OPENAI_API_KEY=<your key>
OPENAI_MODEL=gpt-6-luna
GEAR_API_BASE_URL=https://gearapp.ae:5001
GEAR_API_PREFIX=/Gear/api
REQUEST_TIMEOUT=15
SESSION_TTL_SECONDS=1800
# Add both for production or multi-instance staging:
# UPSTASH_REDIS_REST_URL=https://your-db.upstash.io
# UPSTASH_REDIS_REST_TOKEN=<writable REST token>
CORS_ALLOW_ORIGINS=https://your-web-app.com,http://localhost:3000
```

`HOST` and `PORT` are optional on Vercel (the platform sets routing).
Production (`APP_ENV=production`) requires both Upstash variables. Staging can run
without them for a short demo, but its in-memory conversations can reset between
requests or deployments. Keep the Redis token server-side only.

---

## Option B — CLI from your machine

```bash
cd /path/to/gearappchatbot
npx vercel login
npx vercel link          # pick Erenishere / GEARCHATBOT
chmod +x scripts/push_env_to_vercel.sh
./scripts/push_env_to_vercel.sh
npx vercel --prod        # or redeploy from dashboard
```

The script only uploads to Vercel; it does not touch git.

---

## Checklist

| Item | On GitHub? | On Vercel? |
|------|------------|------------|
| `.env` | No (ignored) | No (use Env Variables UI) |
| `.env.example` | Yes | Copy values manually or import `.env` |
| `OPENAI_API_KEY` | Never | Yes (encrypted) |

After env is set, verify: `GET https://<your-app>.vercel.app/health` and `GET /api/v1/agent/meta`.
