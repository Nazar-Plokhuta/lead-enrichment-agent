"""
Async CLI orchestration entry point for the lead-enrichment agent.

Execution model
---------------
Each URL is handed to ``EnrichmentService.enrich_url``, which runs the shared
pipeline (pending record → scrape → LLM extraction → persist, or ``FAILED``
with the error recorded).  The same service backs the REST API, so both entry
points have identical semantics; this module only owns argument parsing and
Rich presentation.

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

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.config import get_repository, get_settings
from src.llm.client import LLMClient
from src.services.enrichment import EnrichmentService
from src.storage.base import BaseLeadRepository

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
# Results table
# ---------------------------------------------------------------------------


async def _print_results_table(repo: BaseLeadRepository, urls: list[str]) -> None:
    """Query enriched results for *urls* and render a coloured Rich table.

    Rows arrive ordered by ``fit_score`` descending so the highest-value leads
    appear first, regardless of the original URL submission order.  Failed or
    skipped rows appear at the bottom with a ``—`` score.

    Output is written to ``_console`` (stdout) so it does not interleave with
    the log stream on stderr.

    Args:
        repo:    Repository used to fetch the batch's result rows.
        urls:    The exact list of URLs submitted in this run; scopes the
                 query to the current batch rather than the full history.
    """
    rows = await repo.get_leads_by_urls(urls)

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
            "green"
            if status == "PROCESSED"
            else "red"
            if status == "FAILED"
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
# CLI entry point
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct and return the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="lead-enrichment-agent",
        description=(
            "Scrape, enrich, and score B2B company URLs using structured "
            "LLM extraction.  Results are persisted to SQLite or PostgreSQL."
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

    repo = get_repository(settings)
    # Ensure schema (and, for Postgres, the connection pool) is ready before
    # any pipeline task runs.
    await repo.init_db()
    try:
        await _run(repo, urls, force)
    finally:
        await repo.close()


async def _run(repo: BaseLeadRepository, urls: list[str], force: bool) -> None:
    """Fan out the pipeline over *urls* and print the summary and results table."""
    service = EnrichmentService(
        repo,
        LLMClient(settings),
        asyncio.Semaphore(settings.MAX_CONCURRENT_SCRAPES),
        page_timeout_ms=settings.PAGE_TIMEOUT_MS,
    )

    # Fan out: schedule all URLs concurrently, bounded by the service's semaphore.
    tasks = [asyncio.create_task(service.enrich_url(url, force=force)) for url in urls]

    results: list[Literal["processed", "skipped", "failed"]] = await asyncio.gather(
        *tasks
    )

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
    await _print_results_table(repo, urls)
    _console.print()

    if failed_count > 0:
        logger.warning(
            "%d URL(s) failed enrichment — check the database 'error_message' column for details.",
            failed_count,
        )


if __name__ == "__main__":
    asyncio.run(main())
