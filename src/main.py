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

from src.config import get_settings
from src.core.exceptions import LLMExtractionError, ScrapeError
from src.llm.client import LLMClient
from src.scraper.fetcher import fetch_page_markdown
from src.storage.database import init_db
from src.storage.repository import LeadRepository

# ---------------------------------------------------------------------------
# Logging bootstrap
# ---------------------------------------------------------------------------

settings = get_settings()

logging.basicConfig(
    level=settings.LOG_LEVEL.upper(),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

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
    per-URL pipeline tasks under a shared semaphore.  A console summary is
    printed once all tasks complete.
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
    # Console summary
    # -----------------------------------------------------------------
    processed_count = results.count("processed")
    skipped_count = results.count("skipped")
    failed_count = results.count("failed")

    print("\n" + "=" * 60)
    print("  Lead Enrichment Agent — Run Summary")
    print("=" * 60)
    print(f"  Total URLs submitted : {len(urls)}")
    print(f"  Successfully enriched: {processed_count}")
    print(f"  Failed               : {failed_count}")
    print(f"  Skipped (processed)  : {skipped_count}")
    print("=" * 60 + "\n")

    if failed_count > 0:
        logger.warning(
            "%d URL(s) failed enrichment — check the database 'error_message' column for details.",
            failed_count,
        )


if __name__ == "__main__":
    asyncio.run(main())
