import logging
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.exceptions import HTTPException

from app.api.v1.chat import router as chat_router
from app.config import get_settings

settings = get_settings()
app = FastAPI(
    title=f"{settings.app_name} API",
    version="0.1.0",
    debug=settings.debug,
)
app.include_router(chat_router)

if settings.cors_allow_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allow_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-AI-Session-ID",
            "X-AI-Session-Transport",
        ],
    )

logger = logging.getLogger("gear_ai.requests")


@app.get("/playground", include_in_schema=False)
@app.get("/chat", include_in_schema=False)
async def playground() -> HTMLResponse:
    if settings.app_env == "production":
        raise HTTPException(status_code=404)
    html = (Path(__file__).resolve().parent / "static" / "playground.html").read_text()
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.middleware("http")
async def request_log(request: Request, call_next):
    request_id = str(uuid4())
    started = perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
        request_id, request.method, request.url.path, response.status_code,
        (perf_counter() - started) * 1000,
    )
    return response


@app.get("/")
async def root(request: Request):
    accept = request.headers.get("accept", "")
    if settings.app_env != "production" and "text/html" in accept:
        return RedirectResponse("/chat", status_code=307)
    payload = {
        "service": "Gear AI Agent API",
        "status": "running",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/health",
        "agent_meta": "/api/v1/agent/meta",
        "chat": "POST /api/v1/agent/chat",
        "handover": "See docs/APP_DEV_HANDOVER.md in the repository",
    }
    if settings.app_env != "production":
        payload["playground"] = "/chat"
    return payload


@app.get("/health")
async def health():
    return {
        "status": "healthy",
    }
