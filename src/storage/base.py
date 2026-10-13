"""
Storage contract shared by every persistence backend.

The orchestrator depends only on ``BaseLeadRepository``; concrete drivers
(SQLite, PostgreSQL) own all SQL and dialect differences.  Keeping the contract
here lets the backend be swapped through configuration without touching the
pipeline code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.llm.schemas import EnrichedLeadPayload


class BaseLeadRepository(ABC):
    """Async persistence interface for the ``leads`` table."""

    @abstractmethod
    async def init_db(self) -> None:
        """Prepare the backend: create schema objects and acquire resources.

        Must be idempotent and must be awaited once before any other method.
        """

    async def close(self) -> None:
        """Release resources acquired by ``init_db``.

        Backends that open a fresh connection per call (SQLite) hold nothing
        and inherit this no-op; pooled backends override it.
        """

    @abstractmethod
    async def get_lead_by_url(self, url: str) -> dict[str, Any] | None:
        """Return the full lead row for *url*, or ``None`` if absent."""

    @abstractmethod
    async def get_lead_by_id(self, lead_id: int) -> dict[str, Any] | None:
        """Return the API-facing lead row for *lead_id*, or ``None`` if absent.

        The row carries every column except ``raw_markdown``, which is an
        audit artifact too large to ship in API responses.
        """

    @abstractmethod
    async def list_leads(
        self,
        skip: int = 0,
        limit: int = 50,
        fit_tier: str | None = None,
        status: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return one page of leads (newest first) and the total match count.

        *fit_tier* and *status* are exact-match filters; ``None`` disables a
        filter.  The total ignores *skip* and *limit* so callers can paginate.
        Rows have the same shape as ``get_lead_by_id``.
        """

    @abstractmethod
    async def ping(self) -> bool:
        """Verify the backend is reachable.

        Returns ``True`` on success; connectivity failures propagate as
        exceptions so that callers can log the cause.
        """

    @abstractmethod
    async def insert_pending_lead(self, url: str, domain: str) -> int:
        """Create a ``PENDING`` lead, or return the ID of the existing row.

        Must be safe under concurrent calls with the same *url*.
        """

    @abstractmethod
    async def reset_to_pending(self, lead_id: int) -> None:
        """Set the status back to ``PENDING`` and clear ``error_message``.

        Called when an existing lead is queued for another run, so pollers do
        not read the previous run's outcome.  Previously enriched data is kept
        until the new run overwrites it.
        """

    @abstractmethod
    async def save_lead_success(
        self,
        lead_id: int,
        payload: EnrichedLeadPayload,
        raw_markdown: str,
    ) -> None:
        """Persist the enriched payload and set the status to ``PROCESSED``."""

    @abstractmethod
    async def mark_lead_failed(self, lead_id: int, error_message: str) -> None:
        """Set the status to ``FAILED`` and record *error_message*."""

    @abstractmethod
    async def get_leads_by_urls(self, urls: list[str]) -> list[dict[str, Any]]:
        """Return summary rows for *urls*, ordered by ``fit_score`` descending.

        Each row carries ``url``, ``company_name``, ``fit_score``, ``fit_tier``
        and ``status``.  Rows without a score sort last.  Unknown URLs are
        omitted.
        """
