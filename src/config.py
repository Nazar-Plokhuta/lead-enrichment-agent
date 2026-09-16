"""
Application-wide settings loaded from environment variables / .env file.

A single cached instance is returned by `get_settings()` to avoid re-parsing
the environment on every call.  All secrets are wrapped in `SecretStr` so they
are never leaked in tracebacks or log output.
"""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated runtime configuration for the lead-enrichment agent."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM ---
    OPENAI_API_KEY: SecretStr
    OPENAI_MODEL: str = "gpt-4o-mini"

    # --- Persistence ---
    DATABASE_PATH: str = "leads.db"

    # --- Concurrency & timeouts ---
    MAX_CONCURRENT_SCRAPES: int = 3
    PAGE_TIMEOUT_MS: int = 20_000

    # --- Observability ---
    LOG_LEVEL: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the application settings singleton.

    The result is cached after the first call so `.env` is parsed exactly once
    per process lifetime.  Call `get_settings.cache_clear()` in tests to force
    re-evaluation with a patched environment.
    """
    return Settings()
