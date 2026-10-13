"""
Async PostgreSQL repository backed by an ``asyncpg`` connection pool.

Unlike the SQLite driver, which opens a connection per call, this driver
shares one ``asyncpg.Pool`` across all coroutines; the pool is created in
``init_db`` and must be released with ``close``.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from src.llm.schemas import EnrichedLeadPayload
from src.storage.base import BaseLeadRepository

logger = logging.getLogger(__name__)

# Mirrors the SQLite schema.  ``enriched_data`` is JSONB so it can be indexed
# and queried natively, and timestamps are timezone-aware.
_DDL = """
CREATE TABLE IF NOT EXISTS leads (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    url             TEXT        NOT NULL UNIQUE,
    domain          TEXT        NOT NULL,
    company_name    TEXT,
    fit_score       INTEGER,
    fit_tier        TEXT,
    enriched_data   JSONB,
    raw_markdown    TEXT,
    status          TEXT        NOT NULL DEFAULT 'PENDING'
                                CHECK (status IN ('PENDING', 'PROCESSED', 'FAILED')),
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_leads_domain    ON leads(domain);
CREATE INDEX IF NOT EXISTS idx_leads_fit_score ON leads(fit_score);
"""

# Everything except ``raw_markdown`` (see the SQLite driver for the rationale).
_API_COLUMNS = (
    "id, url, domain, company_name, fit_score, fit_tier, enriched_data, "
    "status, error_message, created_at, updated_at"
)


class PostgresLeadRepository(BaseLeadRepository):
    """PostgreSQL implementation of ``BaseLeadRepository``.

    Args:
        dsn:      PostgreSQL connection string (``postgresql://user:pw@host/db``).
        min_size: Minimum number of pooled connections.
        max_size: Maximum number of pooled connections.
    """

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    @property
    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgresLeadRepository.init_db() must be awaited before use")
        return self._pool

    async def init_db(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self._dsn, min_size=self._min_size, max_size=self._max_size
            )
        # Multi-statement DDL is allowed because no parameters are bound.
        await self._require_pool.execute(_DDL)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def get_lead_by_url(self, url: str) -> dict[str, Any] | None:
        row = await self._require_pool.fetchrow("SELECT * FROM leads WHERE url = $1", url)
        return dict(row) if row is not None else None

    async def insert_pending_lead(self, url: str, domain: str) -> int:
        pool = self._require_pool
        lead_id: int | None = await pool.fetchval(
            """
            INSERT INTO leads (url, domain, status)
            VALUES ($1, $2, 'PENDING')
            ON CONFLICT (url) DO NOTHING
            RETURNING id
            """,
            url,
            domain,
        )
        if lead_id is None:
            # Conflict path: RETURNING yields nothing, so resolve the existing row.
            lead_id = await pool.fetchval("SELECT id FROM leads WHERE url = $1", url)
        if lead_id is None:
            raise RuntimeError(f"Lead row missing after upsert for url='{url}'")

        logger.debug("Lead record resolved | url='%s' id=%d", url, lead_id)
        return lead_id

    async def reset_to_pending(self, lead_id: int) -> None:
        await self._require_pool.execute(
            """
            UPDATE leads
            SET
                status        = 'PENDING',
                error_message = NULL,
                updated_at    = now()
            WHERE id = $1
            """,
            lead_id,
        )

    async def save_lead_success(
        self,
        lead_id: int,
        payload: EnrichedLeadPayload,
        raw_markdown: str,
    ) -> None:
        # asyncpg's default jsonb codec takes JSON text, so the validated DTO is
        # serialised by Pydantic and cast server-side; no manual JSON handling.
        await self._require_pool.execute(
            """
            UPDATE leads
            SET
                company_name  = $1,
                fit_score     = $2,
                fit_tier      = $3,
                enriched_data = $4::jsonb,
                raw_markdown  = $5,
                status        = 'PROCESSED',
                error_message = NULL,
                updated_at    = now()
            WHERE id = $6
            """,
            payload.company_name,
            payload.scoring.fit_score,
            payload.scoring.fit_tier,
            payload.model_dump_json(),
            raw_markdown,
            lead_id,
        )
        logger.info(
            "Lead persisted | id=%d company='%s' score=%d tier='%s'",
            lead_id,
            payload.company_name,
            payload.scoring.fit_score,
            payload.scoring.fit_tier,
        )

    async def mark_lead_failed(self, lead_id: int, error_message: str) -> None:
        await self._require_pool.execute(
            """
            UPDATE leads
            SET
                status        = 'FAILED',
                error_message = $1,
                updated_at    = now()
            WHERE id = $2
            """,
            error_message,
            lead_id,
        )
        logger.warning("Lead marked FAILED | id=%d reason='%s'", lead_id, error_message)

    async def get_lead_by_id(self, lead_id: int) -> dict[str, Any] | None:
        row = await self._require_pool.fetchrow(
            f"SELECT {_API_COLUMNS} FROM leads WHERE id = $1", lead_id
        )
        return dict(row) if row is not None else None

    async def list_leads(
        self,
        skip: int = 0,
        limit: int = 50,
        fit_tier: str | None = None,
        status: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        # Only fixed SQL fragments are interpolated; filter values are bound.
        clauses: list[str] = []
        params: list[Any] = []
        if fit_tier is not None:
            params.append(fit_tier)
            clauses.append(f"fit_tier = ${len(params)}")
        if status is not None:
            params.append(status)
            clauses.append(f"status = ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        pool = self._require_pool
        total: int = await pool.fetchval(f"SELECT COUNT(*) FROM leads {where}", *params)
        rows = await pool.fetch(
            f"SELECT {_API_COLUMNS} FROM leads {where} "
            f"ORDER BY id DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}",
            *params,
            limit,
            skip,
        )
        return [dict(row) for row in rows], total

    async def ping(self) -> bool:
        return await self._require_pool.fetchval("SELECT 1") == 1

    async def get_leads_by_urls(self, urls: list[str]) -> list[dict[str, Any]]:
        if not urls:
            return []
        rows = await self._require_pool.fetch(
            """
            SELECT url, company_name, fit_score, fit_tier, status
            FROM leads
            WHERE url = ANY($1::text[])
            ORDER BY fit_score DESC NULLS LAST
            """,
            urls,
        )
        return [dict(row) for row in rows]
