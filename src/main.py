"""
Async CLI orchestration entry point for the lead-enrichment agent.

Execution model
---------------
Each URL is processed through a five-stage pipeline:

    1. ``LeadRepository.insert_pending_lead`` — creates (or retrieves) the
       database record; establishes the lead_id used by all subsequent stages.
    2. ``fetch_page_markdown`` — headless browser scrape + DOM normalisation.
    3. ``LLMClient.enrich_lead`` — structured-output extraction via OpenAI.
    4. ``LeadRepository.save_lead_success`` — persist enriched payload.
    5. On ``ScrapeError`` or ``LLMExtractionError``: ``mark_lead_failed`` so
       the record is queryable for post-mortem analysis, then continue.

Concurrency
-----------
An ``asyncio.Semaphore`` bounded by ``settings.MAX_CONCURRENT_SCRAPES``
prevents unbounded parallelism that would exhaust browser-process limits and
OpenAI rate quotas.  Each URL task acquires the semaphore before launching
Playwright and releases it immediately after the pipeline completes or fails.

Idempotency
-----------
URLs whose database record already carries ``status='PROCESSED'`` are skipped
on repeat runs.  Pass ``--force`` to override this guard and re-enrich all
supplied URLs regardless of prior state.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Literal
from urllib.parse import urlparse

import aiosqlite
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.config import get_settings
from src.core.exceptions import LLMExtractionError, ScrapeError
from src.llm.client import LLMClient
from src.scraper.fetcher import fetch_page_markdown
from src.storage.database import init_db
from src.storage.repository import LeadRepository

# ---------------------------------------------------------------------------
# Console & logging bootstrap
# ---------------------------------------------------------------------------

settings = get_settings()

# Structured output (summary panel, results table) goes to stdout so that
# piped workflows can separate machine-readable signal from log noise.
_console = Console()

# Logs route to stderr via RichHandler; this keeps stdout clean for
# downstream consumers (jq, tee, etc.).  The format string is intentionally
# minimal — Rich renders level labels and timestamps natively with colour.
logging.basicConfig(
    level=settings.LOG_LEVEL.upper(),
    format="%(message)s",
    datefmt="[%X]",
    handlers=[
        RichHandler(
            console=Console(stderr=True),
            rich_tracebacks=True,
            show_path=False,
            markup=False,
            log_time_format="[%X]",
            # Disable Rich's regex-based auto-highlighter so that quoted
            # strings, numbers, and URLs inside log messages are not
            # coloured; only the level badge and timestamp get styling.
            highlighter=None,
        )
    ],
)
logger = logging.getLogger(__name__)
# Prevent the root basicConfig handler from being inherited a second time
# if this module is imported by a wrapper (e.g. scripts/demo_run.py).
logger.propagate = True

# ---------------------------------------------------------------------------
# Tier → colour mapping (mirrors LeadScoring.fit_tier Literal values)
# ---------------------------------------------------------------------------

_TIER_STYLES: dict[str, str] = {
    "Tier 1 (High)": "bold green",
    "Tier 2 (Medium)": "bold bright_yellow",
    "Tier 3 (Low)": "bold cyan",
    "Disqualified": "bold red",  # vibrant, not dim — dim renders as murky grey
}

# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------


def _extract_domain(url: str) -> str:
    """Parse the registered domain from a fully-qualified URL.

    Uses ``urllib.parse.urlparse`` so that scheme, path, query-string, and
    fragment components are stripped, leaving only the ``netloc``.  A leading
    ``www.`` prefix is preserved intentionally — the domain column is used for
    grouping, not deduplication at the TLD level.

    Args:
        url: Fully-qualified URL (e.g. ``"https://www.example.com/about"``).

    Returns:
        The ``netloc`` component of the URL (e.g. ``"www.example.com"``).
    """
    return urlparse(url).netloc


# ---------------------------------------------------------------------------
# Results table
# ---------------------------------------------------------------------------


async def _print_results_table(db_path: str, urls: list[str]) -> None:
    """Query enriched results for *urls* and render a coloured Rich table.

    Rows are ordered by ``fit_score`` descending so the highest-value leads
    appear first, regardless of the original URL submission order.  Failed or
    skipped rows appear at the bottom with a ``—`` score.

    Output is written to ``_console`` (stdout) so it does not interleave with
    the log stream on stderr.

    Args:
        db_path: Filesystem path to the SQLite database.
        urls:    The exact list of URLs submitted in this run; scopes the
                 query to the current batch rather than the full history.
    """
    placeholders = ", ".join("?" * len(urls))
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            f"SELECT url, company_name, fit_score, fit_tier, status "
            f"FROM leads WHERE url IN ({placeholders}) "
            f"ORDER BY fit_score DESC NULLS LAST",
            urls,
        ) as cursor:
            rows = await cursor.fetchall()

    table = Table(
        title="[bold white]Enrichment Results[/bold white]",
        show_header=True,
        header_style="bold white on grey23",
        border_style="bright_black",
        expand=False,
        show_lines=False,
    )
    table.add_column("Company", min_width=22, max_width=32, no_wrap=True)
    table.add_column("Score", justify="right", min_width=5)
    table.add_column("Tier", min_width=20)
    table.add_column("Status", min_width=11)

    for row in rows:
        company: str = row["company_name"] or "—"
        score: str = str(row["fit_score"]) if row["fit_score"] is not None else "—"
        tier_str: str = row["fit_tier"] or "—"
        status: str = row["status"] or "—"

        status_style = (
            "green" if status == "PROCESSED"
            else "red" if status == "FAILED"
            else "yellow"
        )

        table.add_row(
            Text(company, style="bold white"),
            score,
            Text(tier_str, style=_TIER_STYLES.get(tier_str, "white")),
            Text(status, style=status_style),
        )

    _console.print(table)


# ---------------------------------------------------------------------------
# Per-URL pipeline
# ---------------------------------------------------------------------------


async def _process_url(
    url: str,
    *,
    repo: LeadRepository,
    llm_client: LLMClient,
    semaphore: asyncio.Semaphore,
    force: bool,
) -> Literal["processed", "skipped", "failed"]:
    """Run the full enrichment pipeline for a single URL.

    Acquires the semaphore before any I/O so that at most
    ``settings.MAX_CONCURRENT_SCRAPES`` pipelines run concurrently.

    Args:
        url:        The company URL to enrich.
        repo:       Shared ``LeadRepository`` instance for all database ops.
        llm_client: Shared ``LLMClient`` instance for OpenAI calls.
        semaphore:  Concurrency gate shared across all URL tasks.
        force:      When ``True``, re-enrich URLs already in ``'PROCESSED'``
                    state; when ``False``, skip them.

    Returns:
        A ``Literal`` outcome string:
        - ``"processed"`` — pipeline completed and payload persisted.
        - ``"skipped"``   — URL was already ``'PROCESSED'`` and ``--force``
                            was not supplied; no work performed.
        - ``"failed"``    — a ``ScrapeError`` or ``LLMExtractionError`` was
                            raised; the database record is marked ``'FAILED'``.
    """
    # -----------------------------------------------------------------
    # Idempotency guard — check before acquiring the semaphore so that
    # already-processed URLs consume zero concurrency slots.
    # -----------------------------------------------------------------
    existing = await repo.get_lead_by_url(url)
    if existing and existing["status"] == "PROCESSED" and not force:
        logger.info("Skipping already-processed URL (use --force to re-enrich): %s", url)
        return "skipped"

    async with semaphore:
        domain = _extract_domain(url)
        lead_id: int | None = None

        try:
            # Stage 1 — ensure a database record exists and obtain its PK.
            lead_id = await repo.insert_pending_lead(url, domain)
            logger.info("Pipeline start | url='%s' id=%d", url, lead_id)

            # Stage 2 — scrape: headless browser → clean Markdown.
            markdown = await fetch_page_markdown(url, timeout_ms=settings.PAGE_TIMEOUT_MS)

            # Stage 3 — enrich: Markdown → validated EnrichedLeadPayload.
            payload = await llm_client.enrich_lead(url, markdown)

            # Stage 4 — persist: structured payload → SQLite.
            await repo.save_lead_success(lead_id, payload, markdown)

            logger.info(
                "Pipeline success | url='%s' company='%s' score=%d tier='%s'",
                url,
                payload.company_name,
                payload.scoring.fit_score,
                payload.scoring.fit_tier,
            )
            return "processed"

        except (ScrapeError, LLMExtractionError) as exc:
            # Both error types carry a ``reason`` attribute with a
            # human-readable failure description suitable for storage.
            error_msg = str(exc)
            logger.error("Pipeline failure | url='%s' error='%s'", url, error_msg)

            if lead_id is not None:
                # Best-effort: persist failure state so the record is
                # queryable for re-runs or post-mortem analysis.
                await repo.mark_lead_failed(lead_id, error_msg)

            return "failed"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct and return the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="lead-enrichment-agent",
        description=(
            "Scrape, enrich, and score B2B company URLs using structured "
            "LLM extraction.  Results are persisted to SQLite."
        ),
    )
    parser.add_argument(
        "urls",
        nargs="+",
        metavar="URL",
        help="One or more fully-qualified company URLs to enrich.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help=(
            "Re-enrich URLs that are already in PROCESSED state.  "
            "Without this flag, processed URLs are skipped on re-runs."
        ),
    )
    return parser


async def main() -> None:
    """Async orchestration entry point.

    Parses CLI arguments, initialises the database schema, and fans out
    per-URL pipeline tasks under a shared semaphore.  A Rich summary panel
    and a coloured results table are printed once all tasks complete.
    """
    parser = _build_arg_parser()
    args = parser.parse_args()

    urls: list[str] = args.urls
    force: bool = args.force

    logger.info(
        "Starting lead enrichment | urls=%d force=%s concurrency=%d",
        len(urls),
        force,
        settings.MAX_CONCURRENT_SCRAPES,
    )

    # Ensure schema is in place before any pipeline task runs.
    await init_db(settings.DATABASE_PATH)

    repo = LeadRepository(settings.DATABASE_PATH)
    llm_client = LLMClient(settings)
    semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_SCRAPES)

    # Fan out: schedule all URLs concurrently, bounded by the semaphore.
    tasks = [
        asyncio.create_task(
            _process_url(
                url,
                repo=repo,
                llm_client=llm_client,
                semaphore=semaphore,
                force=force,
            )
        )
        for url in urls
    ]

    results: list[Literal["processed", "skipped", "failed"]] = await asyncio.gather(*tasks)

    # -----------------------------------------------------------------
    # Run summary panel
    # -----------------------------------------------------------------
    processed_count = results.count("processed")
    skipped_count = results.count("skipped")
    failed_count = results.count("failed")

    summary_lines = [
        f"  [white]Total submitted :[/white]  [bold]{len(urls)}[/bold]",
        f"  [white]Enriched        :[/white]  [bold green]{processed_count}[/bold green]",
        f"  [white]Failed          :[/white]  [bold red]{failed_count}[/bold red]",
        f"  [white]Skipped         :[/white]  [bold yellow]{skipped_count}[/bold yellow]",
    ]
    _console.print()
    _console.print(
        Panel(
            "\n".join(summary_lines),
            title="[bold]Lead Enrichment Agent — Run Summary[/bold]",
            border_style="bright_black",
            expand=False,
            padding=(0, 1),
        )
    )

    # -----------------------------------------------------------------
    # Per-lead coloured results table
    # -----------------------------------------------------------------
    _console.print()
    await _print_results_table(settings.DATABASE_PATH, urls)
    _console.print()

    if failed_count > 0:
        logger.warning(
            "%d URL(s) failed enrichment — check the database 'error_message' column for details.",
            failed_count,
        )


if __name__ == "__main__":
    asyncio.run(main())
