"""Server-side configuration; secrets are read only when a client needs them."""

from dataclasses import dataclass
from functools import lru_cache
import math
import os

from dotenv import load_dotenv


class ConfigurationError(Exception):
    """A required integration setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    openai_model: str
    openai_api_key: str
    gear_api_base_url: str
    gear_api_prefix: str
    request_timeout: float
    session_ttl_seconds: int
    app_name: str = "Gear AI Agent"
    app_env: str = "development"
    debug: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    demo_mode: bool = False
    demo_extractor: str = "local"
    cors_allow_origins: tuple[str, ...] = ()
    upstash_redis_rest_url: str = ""
    upstash_redis_rest_token: str = ""

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv(override=False)
        try:
            timeout = float(os.getenv("REQUEST_TIMEOUT", "15"))
            ttl = int(os.getenv("SESSION_TTL_SECONDS", "1800"))
            port = int(os.getenv("PORT", "8000"))
        except ValueError as exc:
            raise ConfigurationError("Timeout, session TTL and port must be numeric") from exc
        if not math.isfinite(timeout) or timeout <= 0 or ttl <= 0 or not 1 <= port <= 65535:
            raise ConfigurationError("Timeout, session TTL or port is out of range")
        debug_value = os.getenv("DEBUG", "false").strip().lower()
        if debug_value not in {"true", "false", "1", "0"}:
            raise ConfigurationError("DEBUG must be true or false")
        demo_value = os.getenv("DEMO_MODE", "false").strip().lower()
        if demo_value not in {"true", "false", "1", "0"}:
            raise ConfigurationError("DEMO_MODE must be true or false")
        demo_extractor = os.getenv("DEMO_EXTRACTOR", "local").strip().lower()
        if demo_extractor not in {"local", "hosted"}:
            raise ConfigurationError("DEMO_EXTRACTOR must be local or hosted")
        app_env = os.getenv("APP_ENV", "development").strip().lower()
        if app_env not in {"development", "staging", "production"}:
            raise ConfigurationError("APP_ENV must be development, staging, or production")
        debug = debug_value in {"true", "1"}
        demo_mode = demo_value in {"true", "1"}
        if app_env in {"production", "staging"} and debug:
            raise ConfigurationError("DEBUG must be false in staging and production")
        if app_env == "production" and demo_mode:
            raise ConfigurationError("DEMO_MODE must be false in production")
        cors_raw = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
        cors_allow_origins = tuple(
            origin.strip() for origin in cors_raw.split(",") if origin.strip()
        )
        prefix = os.getenv("GEAR_API_PREFIX", "/Gear/api").strip()
        if not prefix.startswith("/") or "//" in prefix or ".." in prefix:
            raise ConfigurationError("GEAR_API_PREFIX must be an absolute URL path")
        redis_url = os.getenv("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
        redis_token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
        if bool(redis_url) != bool(redis_token):
            raise ConfigurationError("Both Upstash REST URL and token must be configured")
        if app_env == "production" and not redis_url:
            raise ConfigurationError("Production requires shared sessions (Upstash Redis REST)")
        return cls(
            openai_model=os.getenv("OPENAI_MODEL", "gpt-6-luna").strip(),
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            gear_api_base_url=os.getenv("GEAR_API_BASE_URL", "").strip().rstrip("/"),
            gear_api_prefix=prefix.rstrip("/"),
            request_timeout=timeout,
            session_ttl_seconds=ttl,
            app_name=os.getenv("APP_NAME", "Gear AI Agent").strip() or "Gear AI Agent",
            app_env=app_env,
            debug=debug,
            host=os.getenv("HOST", "127.0.0.1").strip(),
            port=port,
            demo_mode=demo_mode,
            demo_extractor=demo_extractor,
            cors_allow_origins=cors_allow_origins,
            upstash_redis_rest_url=redis_url,
            upstash_redis_rest_token=redis_token,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_environment()
