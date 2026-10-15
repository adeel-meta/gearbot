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
