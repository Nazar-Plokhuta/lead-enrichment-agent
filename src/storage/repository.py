"""
Async CRUD repository for the `leads` table.

Design intent
-------------
``LeadRepository`` is the **only** module in the application that executes SQL
against the leads table.  It has no knowledge of the scraper or the LLM layer;
it receives validated Python values and persists them — nothing more.

Each method opens a fresh connection via ``get_db_connection`` so that
repository instances are safely sharable across concurrent coroutines without
lock contention.  The connection context manager guarantees closure even when
an exception propagates, preventing file-handle leaks.

Transaction safety
------------------
Every mutating operation calls ``await conn.commit()`` explicitly.
``aiosqlite`` does **not** auto-commit DML; omitting the commit would leave
writes invisible to other connections and silently rolled back on close.
"""

from __future__ import annotations

import logging

from src.llm.schemas import EnrichedLeadPayload
from src.storage.database import get_db_connection

logger = logging.getLogger(__name__)


class LeadRepository:
    """Async data-access object for lead persistence.

    All methods are coroutines and must be awaited.  The repository is
    intentionally stateless — it holds only the database path so that the
    same instance can be shared across concurrent pipeline tasks.

    Args:
        db_path: Filesystem path to the SQLite database file.  Typically
                 ``settings.DATABASE_PATH``.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_lead_by_url(self, url: str) -> dict | None:  # type: ignore[type-arg]
        """Fetch a single lead row by its URL.

        Used by the orchestrator's idempotency guard to determine whether a URL
        has already been enriched and can be skipped on re-runs.

        Args:
            url: The canonical URL of the target company page.

        Returns:
            A plain ``dict`` representation of the row, or ``None`` if no
            record exists for the given URL.
        """
        async with get_db_connection(self._db_path) as conn, conn.execute(
            "SELECT * FROM leads WHERE url = ?",
            (url,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return None

        # ``aiosqlite.Row`` supports ``dict()`` conversion via its mapping
        # interface; this keeps the caller free from aiosqlite internals.
        return dict(row)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def insert_pending_lead(self, url: str, domain: str) -> int:
        """Insert a new lead record with status ``'PENDING'``, or retrieve the
        existing row's ID if the URL already exists in the table.

        ``INSERT OR IGNORE`` avoids a race condition in concurrent pipelines:
        two workers may attempt to insert the same URL simultaneously, and
        the constraint guarantees only one row is created.  The subsequent
        ``SELECT`` retrieves the authoritative ID regardless of whether the
        INSERT fired or was suppressed by the conflict guard.

        Args:
            url:    Fully-qualified URL of the target company page (used as the
                    UNIQUE key in the leads table).
            domain: Registered domain extracted from the URL (e.g.
                    ``"example.com"``).  Stored for domain-level analytics.

        Returns:
            The integer primary-key ``id`` of the lead row.
        """
        async with get_db_connection(self._db_path) as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO leads (url, domain, status)
                VALUES (?, ?, 'PENDING')
                """,
                (url, domain),
            )
            await conn.commit()

            async with conn.execute(
                "SELECT id FROM leads WHERE url = ?",
                (url,),
            ) as cursor:
                row = await cursor.fetchone()

        # The SELECT is guaranteed to return a row: either we just inserted it,
        # or it existed before.  A None result would indicate data corruption.
        assert row is not None, f"Expected lead row for url='{url}' after INSERT OR IGNORE"
        lead_id: int = row["id"]
        logger.debug("Lead record resolved | url='%s' id=%d", url, lead_id)
        return lead_id

    async def save_lead_success(
        self,
        lead_id: int,
        payload: EnrichedLeadPayload,
        raw_markdown: str,
    ) -> None:
        """Persist a fully enriched lead and advance its status to
        ``'PROCESSED'``.

        ``payload.model_dump_json()`` serialises the entire structured output
        into the ``enriched_data`` JSON column, preserving the full extraction
        artifact for downstream consumers.  Top-level scalar fields
        (``company_name``, ``fit_score``, ``fit_tier``) are also stored in
        dedicated columns to allow efficient SQL queries without JSON
        path extraction.

        Args:
            lead_id:      Primary key of the lead row to update.
            payload:      Validated ``EnrichedLeadPayload`` instance produced
                          by ``LLMClient.enrich_lead``.
            raw_markdown: The normalised Markdown string returned by the
                          scraping pipeline; archived for audit and replay.
        """
        async with get_db_connection(self._db_path) as conn:
            await conn.execute(
                """
                UPDATE leads
                SET
                    company_name  = ?,
                    fit_score     = ?,
                    fit_tier      = ?,
                    enriched_data = ?,
                    raw_markdown  = ?,
                    status        = 'PROCESSED',
                    error_message = NULL,
                    updated_at    = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    payload.company_name,
                    payload.scoring.fit_score,
                    payload.scoring.fit_tier,
                    payload.model_dump_json(),
                    raw_markdown,
                    lead_id,
                ),
            )
            await conn.commit()

        logger.info(
            "Lead persisted | id=%d company='%s' score=%d tier='%s'",
            lead_id,
            payload.company_name,
            payload.scoring.fit_score,
            payload.scoring.fit_tier,
        )

    async def mark_lead_failed(self, lead_id: int, error_message: str) -> None:
        """Advance a lead's status to ``'FAILED'`` and record the failure
        reason for post-mortem analysis and selective re-runs.

        This method is intentionally never raises: if a persistence failure
        occurs here the pipeline would otherwise lose the original error
        context.  The caller retains responsibility for logging.

        Args:
            lead_id:       Primary key of the lead row to mark as failed.
            error_message: Human-readable description of the failure that
                           prevented successful enrichment.
        """
        async with get_db_connection(self._db_path) as conn:
            await conn.execute(
                """
                UPDATE leads
                SET
                    status        = 'FAILED',
                    error_message = ?,
                    updated_at    = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error_message, lead_id),
            )
            await conn.commit()

        logger.warning("Lead marked FAILED | id=%d reason='%s'", lead_id, error_message)
