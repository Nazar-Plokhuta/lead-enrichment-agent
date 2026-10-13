"""
Application-wide settings loaded from environment variables / .env file.

A single cached instance is returned by `get_settings()` to avoid re-parsing
the environment on every call.  All secrets are wrapped in `SecretStr` so they
are never leaked in tracebacks or log output.
"""

from functools import lru_cache
from typing import TYPE_CHECKING

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from src.storage.base import BaseLeadRepository

_POSTGRES_SCHEMES = ("postgresql://", "postgres://")


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
    # Optional base URL override — set to route requests through OpenRouter,
    # a self-hosted proxy, or any OpenAI-compatible endpoint.
    OPENAI_BASE_URL: str | None = None

    # --- Persistence ---
    DATABASE_PATH: str = "leads.db"
    # When set, PostgreSQL replaces SQLite.  SecretStr because the DSN embeds
    # credentials.
    DATABASE_URL: SecretStr | None = None

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


def get_repository(settings: Settings | None = None) -> "BaseLeadRepository":
    """Build the repository selected by configuration.

    Returns a ``PostgresLeadRepository`` when ``DATABASE_URL`` is set,
    otherwise a SQLite ``LeadRepository`` on ``DATABASE_PATH``.  The caller
    owns the lifecycle: await ``init_db()`` first and ``close()`` when done.

    Raises:
        ValueError: ``DATABASE_URL`` is set but is not a PostgreSQL DSN.  Falling
            back to SQLite here would silently write to the wrong store.
    """
    settings = settings or get_settings()

    # Deferred import: storage modules import ``src.llm``, which imports this
    # module, so a top-level import would be circular.
    from src.storage.postgres import PostgresLeadRepository
    from src.storage.repository import LeadRepository

    if settings.DATABASE_URL is None:
        return LeadRepository(settings.DATABASE_PATH)

    dsn = settings.DATABASE_URL.get_secret_value()
    if not dsn.startswith(_POSTGRES_SCHEMES):
        raise ValueError("DATABASE_URL must start with 'postgresql://' or 'postgres://'")
    return PostgresLeadRepository(dsn)
