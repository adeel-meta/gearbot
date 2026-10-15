"""One chat endpoint shared by Gear Web and Mobile."""

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response

from app.clients.demo import DemoExtractor, DemoGearClient
from app.clients.gear import GearClient
from app.clients.llm import OpenAIRequirementExtractor
from app.config import get_settings
from app.schemas.chat import ChatRequest, ChatResponse, SessionToken
from app.security.sessions import SessionStore
from app.security.shared_sessions import SessionStoreUnavailable, SharedSessionStore
from app.services.conversation import ConversationService, TurnConflict


router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


@lru_cache(maxsize=1)
def _build_chat_service() -> ConversationService:
    settings = get_settings()
    sessions = (SharedSessionStore(settings.session_ttl_seconds,
                                  settings.upstash_redis_rest_url,
                                  settings.upstash_redis_rest_token)
                if settings.upstash_redis_rest_url
                else SessionStore(settings.session_ttl_seconds))
    if settings.demo_mode:
        return ConversationService(
            extractor=(OpenAIRequirementExtractor(settings)
                       if settings.demo_extractor == "hosted" else DemoExtractor()),
            gear=DemoGearClient(),
            sessions=sessions,
            data_source="demo",
        )
    return ConversationService(
        extractor=OpenAIRequirementExtractor(settings),
        gear=GearClient(settings),
        sessions=sessions,
    )


@router.get("/meta")
async def chat_meta() -> dict[str, object]:
    settings = get_settings()
    return {
        "mode": "demo" if settings.demo_mode else "live",
        "extractor": settings.demo_extractor if settings.demo_mode else "hosted",
        "llm_provider": ("openai" if not settings.demo_mode
                         or settings.demo_extractor == "hosted" else None),
        "gear_configured": bool(settings.gear_api_base_url),
        "customer_token_needed_for_details": not settings.demo_mode,
        "protected_features_require_login": not settings.demo_mode,
    }


async def get_chat_service() -> ConversationService:
    return _build_chat_service()


def bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token or len(token) > 4096:
        raise HTTPException(status_code=401, detail="Invalid bearer authorization")
    if any(character.isspace() for character in token):
        raise HTTPException(status_code=401, detail="Invalid bearer authorization")
    return token


@router.post("/chat", response_model=ChatResponse, response_model_exclude_none=True)
async def chat(
    request: ChatRequest,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
    session_header: Annotated[SessionToken | None, Header(alias="X-AI-Session-ID")] = None,
    session_cookie: Annotated[SessionToken | None, Cookie(alias="ai_session_id")] = None,
    session_transport: Annotated[str | None, Header(alias="X-AI-Session-Transport")] = None,
    service: ConversationService = Depends(get_chat_service),
) -> ChatResponse:
    supplied_ids = {value for value in (request.session_id, session_header, session_cookie)
                    if value is not None}
    if len(supplied_ids) > 1:
        raise HTTPException(status_code=400, detail="Conflicting AI session IDs")
    if session_transport not in {None, "cookie"}:
        raise HTTPException(status_code=400, detail="Unsupported AI session transport")
    session_id = next(iter(supplied_ids), None)
    request = request.model_copy(update={"session_id": session_id})
    token = bearer_token(authorization)
    try:
        result = await service.chat(request, token)
    except TurnConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SessionStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail="Conversation is temporarily unavailable") from exc
    cookie_client = session_transport == "cookie" or session_cookie is not None
    if cookie_client:
        settings = get_settings()
        response.set_cookie(
            key="ai_session_id", value=result.session_id, httponly=True,
            secure=settings.app_env != "development", samesite="lax",
            max_age=settings.session_ttl_seconds, path="/api/v1/agent",
        )
        return result.model_copy(update={"session_id": None})
    return result


@router.post("/session/reset", status_code=204)
async def reset_chat_session(
    response: Response,
    session_header: Annotated[SessionToken | None, Header(alias="X-AI-Session-ID")] = None,
    session_cookie: Annotated[SessionToken | None, Cookie(alias="ai_session_id")] = None,
    service: ConversationService = Depends(get_chat_service),
) -> Response:
    if session_header and session_cookie and session_header != session_cookie:
        raise HTTPException(status_code=400, detail="Conflicting AI session IDs")
    try:
        service.sessions.delete(session_header or session_cookie)
    except SessionStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail="Conversation is temporarily unavailable") from exc
    settings = get_settings()
    response.delete_cookie(
        key="ai_session_id", secure=settings.app_env != "development",
        httponly=True, samesite="lax", path="/api/v1/agent",
    )
    response.status_code = 204
    return response
