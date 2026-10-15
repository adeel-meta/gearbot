"""Deployment-compatible AI conversations backed by Upstash Redis REST.

Only opaque AI-session tokens address records. Gear credentials are never stored.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import secrets
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from pydantic import TypeAdapter, ValidationError

from app.security.sessions import Session, SessionStore


class SessionStoreUnavailable(Exception):
    """Shared session state could not be read or safely committed."""


class SharedSessionStore(SessionStore):
    _adapter = TypeAdapter(Session)
    _release_script = (
        "if redis.call('GET', KEYS[1]) == ARGV[1] then "
        "return redis.call('DEL', KEYS[1]) else return 0 end"
    )

    def __init__(self, ttl_seconds: int, url: str, token: str,
                 transport: httpx.BaseTransport | None = None):
        super().__init__(ttl_seconds)
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("UPSTASH_REDIS_REST_URL must be an HTTPS endpoint")
        if not token:
            raise ValueError("UPSTASH_REDIS_REST_TOKEN is required")
        self.url = url.rstrip("/")
        self.token = token
        self.transport = transport

    @staticmethod
    def _key(session_token: str) -> str:
        # The Redis key contains no bearer token; the token is high-entropy and one-way hashed.
        digest = hashlib.sha256(session_token.encode()).hexdigest()
        return f"gear-ai:session:v1:{digest}"

    def _command(self, command: list[object]) -> object:
        try:
            with httpx.Client(timeout=5, transport=self.transport) as client:
                response = client.post(
                    self.url, json=command,
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                response.raise_for_status()
                envelope = response.json()
            if not isinstance(envelope, dict) or "error" in envelope or "result" not in envelope:
                raise SessionStoreUnavailable("Shared session command failed")
            return envelope["result"]
        except (httpx.HTTPError, ValueError) as exc:
            raise SessionStoreUnavailable("Shared session store unavailable") from exc

    def get_or_create(self, session_id: str | None) -> Session:
        now = datetime.now(timezone.utc)
        if session_id:
            raw = self._command(["GET", self._key(session_id)])
            if raw is not None:
                try:
                    session = self._adapter.validate_json(raw)
                except (ValidationError, ValueError) as exc:
                    raise SessionStoreUnavailable("Stored conversation is invalid") from exc
                if session.public_token != session_id:
                    raise SessionStoreUnavailable("Stored conversation token mismatch")
                if session.expires_at > now:
                    session.expires_at = now + self.ttl
                    return session
        session = Session(id=uuid4(), public_token=self._new_public_token(),
                          expires_at=now + self.ttl,
                          recovered_after_expiry=session_id is not None)
        self.save(session)
        return session

    def save(self, session: Session) -> None:
        seconds = max(1, int((session.expires_at - datetime.now(timezone.utc)).total_seconds()))
        result = self._command(["SET", self._key(session.public_token),
                                self._adapter.dump_json(session).decode(), "EX", seconds])
        if result != "OK":
            raise SessionStoreUnavailable("Shared conversation could not be saved")

    def delete(self, session_token: str | None) -> None:
        if session_token:
            self._command(["DEL", self._key(session_token)])

    async def acquire_turn(self, session_token: str) -> str:
        key = self._key(session_token) + ":turn-lock"
        lease = secrets.token_urlsafe(24)
        # A lease covers the bounded model and Gear timeouts. A lost lease aborts saving.
        for _ in range(80):
            result = self._command(["SET", key, lease, "NX", "EX", 120])
            if result == "OK":
                return lease
            await asyncio.sleep(0.1)
        raise SessionStoreUnavailable("Conversation is busy; please retry")

    def assert_turn_lease(self, session_token: str, lease: str | None) -> None:
        if lease is None or self._command(["GET", self._key(session_token) + ":turn-lock"]) != lease:
            raise SessionStoreUnavailable("Conversation lock expired; please retry")

    async def release_turn(self, session_token: str, lease: str | None) -> None:
        if lease:
            self._command(["EVAL", self._release_script, 1,
                           self._key(session_token) + ":turn-lock", lease])
