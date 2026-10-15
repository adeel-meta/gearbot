"""Short-lived conversations addressed independently from Gear login tokens."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from typing import Literal
from uuid import UUID, uuid4

from app.schemas.chat import ChatResponse, SearchFilters, VehicleCard


@dataclass(frozen=True)
class PendingProtectedOperation:
    kind: Literal["search_availability", "vehicle_availability", "details", "catalog_search"]
    vehicle_id: str | None = None
    request_message: str = ""
    catalog_skipped: bool = False
    criteria_revision: int | None = None


@dataclass
class Session:
    id: UUID
    public_token: str
    expires_at: datetime
    filters: SearchFilters = field(default_factory=SearchFilters)
    results: list[VehicleCard] = field(default_factory=list)
    pending_requirement: Literal[
        "location", "location_then_dates", "dates", "availability_dates",
        "availability_times", "vehicle_type", "budget_period",
    ] | None = None
    pending_budget: bool = False
    search_mode: Literal["guided", "browse"] = "browse"
    anywhere: bool = False
    budget_decided: bool = False
    trip_budget: float | None = None
    location_source: Literal["typed", "shared"] | None = None
    field_provenance: dict[str, str] = field(default_factory=dict)
    last_assistant_action: str | None = None
    last_search_filters: SearchFilters | None = None
    last_search_outcome: Literal["results", "empty", "unavailable"] | None = None
    criteria_revision: int = 0
    results_revision: int | None = None
    recent_turns: list[str] = field(default_factory=list)
    recent_responses: dict[str, tuple[str, ChatResponse]] = field(default_factory=dict)
    recovered_after_expiry: bool = False
    pending_operation: PendingProtectedOperation | None = None
    selected_vehicle_id: str | None = None
    pending_vehicle_intent: Literal["availability"] | None = None


class SessionStore:
    def __init__(self, ttl_seconds: int):
        self.ttl = timedelta(seconds=ttl_seconds)
        self.secret = secrets.token_bytes(32)
        self.sessions: dict[UUID, Session] = {}
        self.session_token_digests: dict[str, UUID] = {}

    def _session_token_digest(self, session_token: str) -> str:
        return hmac.new(self.secret, b"session:" + session_token.encode(),
                        hashlib.sha256).hexdigest()

    def _new_public_token(self) -> str:
        return secrets.token_urlsafe(32)

    def _prune(self, now: datetime) -> None:
        expired = [session_id for session_id, session in self.sessions.items()
                   if session.expires_at <= now]
        for session_id in expired:
            session = self.sessions.pop(session_id)
            self.session_token_digests.pop(self._session_token_digest(session.public_token), None)

    def get_or_create(self, session_id: str | None) -> Session:
        now = datetime.now(timezone.utc)
        self._prune(now)
        if session_id is not None:
            token_digest = self._session_token_digest(session_id)
            internal_id = self.session_token_digests.get(token_digest)
            session = self.sessions.get(internal_id) if internal_id else None
            if session is not None and session.expires_at > now:
                session.expires_at = now + self.ttl
                return session
        public_token = self._new_public_token()
        new_session = Session(id=uuid4(), public_token=public_token,
                              expires_at=now + self.ttl,
                              recovered_after_expiry=session_id is not None)
        self.sessions[new_session.id] = new_session
        self.session_token_digests[self._session_token_digest(public_token)] = new_session.id
        if len(self.sessions) > 1000:
            oldest = min(self.sessions, key=lambda key: self.sessions[key].expires_at)
            removed = self.sessions.pop(oldest)
            self.session_token_digests.pop(self._session_token_digest(removed.public_token), None)
        return new_session

    def delete(self, session_token: str | None) -> None:
        if not session_token:
            return
        digest = self._session_token_digest(session_token)
        internal_id = self.session_token_digests.pop(digest, None)
        if internal_id is not None:
            self.sessions.pop(internal_id, None)

    def save(self, session: Session) -> None:
        """In-memory sessions are already updated by reference."""
        return None

    async def acquire_turn(self, session_token: str) -> str | None:
        """The conversation service supplies a process-local lock for this store."""
        return None

    async def release_turn(self, session_token: str, lease: str | None) -> None:
        return None

    def assert_turn_lease(self, session_token: str, lease: str | None) -> None:
        return None
