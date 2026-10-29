"""Staging and CORS settings used for Vercel handover deploys."""

import os

import pytest

from app.config import ConfigurationError, Settings


def test_staging_allows_demo_mode_without_debug(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("DEMO_MODE", "true")
    settings = Settings.from_environment()
    assert settings.app_env == "staging"
    assert settings.demo_mode is True
    assert settings.debug is False


def test_staging_rejects_debug(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DEBUG", "true")
    with pytest.raises(ConfigurationError, match="DEBUG"):
        Settings.from_environment()


def test_cors_origins_parsed(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://a.test, http://localhost:3000 ")
    settings = Settings.from_environment()
    assert settings.cors_allow_origins == ("https://a.test", "http://localhost:3000")


def test_cors_origins_default_when_unset(monkeypatch):
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    settings = Settings.from_environment()
    assert "http://localhost:5173" in settings.cors_allow_origins
    assert "https://gearapp.ae" in settings.cors_allow_origins


def test_cors_middleware_exposes_headers():
    import asyncio
    import httpx
    from app.main import app

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.options(
                "/api/v1/agent/chat",
                headers={
                    "Origin": "https://gearapp.ae",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Authorization,Content-Type,Accept,X-AI-Session-ID,X-AI-Session-Transport",
                },
            )
            assert res.status_code == 200
            assert res.headers.get("access-control-allow-origin") == "https://gearapp.ae"
            get_res = await client.get("/health", headers={"Origin": "https://gearapp.ae"})
            assert get_res.headers.get("access-control-allow-origin") == "https://gearapp.ae"
            exposed = get_res.headers.get("access-control-expose-headers", "")
            assert "X-AI-Session-ID" in exposed
            assert "X-Request-ID" in exposed

    asyncio.run(scenario())


def test_production_requires_shared_sessions(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    with pytest.raises(ConfigurationError, match="shared sessions"):
        Settings.from_environment()


def test_shared_session_credentials_must_be_configured_together(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://redis.example")
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    with pytest.raises(ConfigurationError, match="Both Upstash"):
        Settings.from_environment()
