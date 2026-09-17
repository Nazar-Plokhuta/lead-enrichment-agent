"""
Async SQLite bootstrap: schema initialisation and connection lifecycle.

Responsibilities
----------------
- `init_db`: idempotent DDL execution (tables + indexes) on first run.
- `get_db_connection`: async context manager that opens a connection, enables
  foreign-key enforcement, and guarantees closure on exit.

All higher-level CRUD lives in `storage/repository.py`; this module owns only
the structural concerns so it can be imported without triggering business logic.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import aiosqlite

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT    NOT NULL UNIQUE,
    domain          TEXT    NOT NULL,
    company_name    TEXT,
    fit_score       INTEGER,
    fit_tier        TEXT,
    enriched_data   JSON,
    raw_markdown    TEXT,
    status          TEXT    CHECK(status IN ('PENDING', 'PROCESSED', 'FAILED'))
                            DEFAULT 'PENDING',
    error_message   TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_leads_domain    ON leads(domain);
CREATE INDEX IF NOT EXISTS idx_leads_fit_score ON leads(fit_score);
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def init_db(db_path: str) -> None:
    """Create the `leads` table and its indexes if they do not already exist.

    Safe to call on every application start; all statements use
    `CREATE … IF NOT EXISTS` so repeated invocations are idempotent.

    Args:
        db_path: Filesystem path to the SQLite database file.
    """
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(_DDL)
        await conn.commit()


@asynccontextmanager
async def get_db_connection(db_path: str) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Yield an open `aiosqlite` connection with foreign-key support enabled.

    Foreign keys are off by default in SQLite and must be activated per
    connection, which is why the PRAGMA is issued immediately after opening.

    Usage::

        async with get_db_connection(settings.DATABASE_PATH) as conn:
            await conn.execute("SELECT 1")

    Args:
        db_path: Filesystem path to the SQLite database file.

    Yields:
        An `aiosqlite.Connection` instance ready for queries.
    """
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = aiosqlite.Row
        yield conn
    finally:
        await conn.close()
