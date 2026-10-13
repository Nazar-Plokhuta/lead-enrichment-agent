"""
Transport-agnostic enrichment pipeline shared by the CLI and the REST API.

Both entry points drive the same two-step contract so that they have identical
semantics regardless of how a job is scheduled:

    1. ``submit``       — idempotency check + ``PENDING`` record creation.
    2. ``process_lead`` — scrape → LLM enrichment → persist (or record failure).

Splitting the steps lets the API answer ``202 Accepted`` as soon as the lead
exists and run ``process_lead`` in a background task, while the CLI simply
awaits both back to back via ``enrich_url``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from src.core.exceptions import LLMExtractionError, ScrapeError
from src.llm.client import LLMClient
from src.scraper.fetcher import fetch_page_markdown
from src.storage.base import BaseLeadRepository

logger = logging.getLogger(__name__)

Outcome = Literal["processed", "skipped", "failed"]
Scraper = Callable[[str, int], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class Submission:
    """Result of ``EnrichmentService.submit``.

    Attributes:
        lead_id:          Primary key of the lead record.
        url:              The submitted URL.
        status:           ``'PENDING'`` when the lead should be processed,
                          otherwise the stored status of the existing record.
        needs_processing: ``False`` when the lead is already ``'PROCESSED'``
                          and was not forced, so ``process_lead`` need not run.
    """

    lead_id: int
    url: str
    status: str
    needs_processing: bool


def extract_domain(url: str) -> str:
    """Return the ``netloc`` of *url*.

    A leading ``www.`` is preserved intentionally — the domain column is used
    for grouping, not deduplication at the TLD level.
    """
    return urlparse(url).netloc


class EnrichmentService:
    """Coordinates repository, scraper and LLM client for one lead at a time.

    Args:
        repo:            Shared repository; its lifecycle belongs to the caller.
        llm_client:      Shared client used for structured-output extraction.
        semaphore:       Concurrency gate bounding simultaneous pipelines.
        page_timeout_ms: Navigation timeout forwarded to the scraper.
        scraper:         Markdown fetcher; injectable so tests never open a
                         browser.
    """

    def __init__(
        self,
        repo: BaseLeadRepository,
        llm_client: LLMClient,
        semaphore: asyncio.Semaphore,
        *,
        page_timeout_ms: int,
        scraper: Scraper = fetch_page_markdown,
    ) -> None:
        self._repo = repo
        self._llm_client = llm_client
        self._semaphore = semaphore
        self._page_timeout_ms = page_timeout_ms
        self._scraper = scraper
        # Lead IDs currently inside ``process_lead``; stops a repeated request
        # for the same URL from running a second concurrent pipeline.
        self._in_flight: set[int] = set()

    async def submit(self, url: str, *, force: bool = False) -> Submission:
        """Create (or retrieve) the lead record and decide whether to process it.

        Runs before the semaphore is taken so that already-processed URLs
        consume no concurrency slot.
        """
        existing = await self._repo.get_lead_by_url(url)
        if existing is not None and existing["status"] == "PROCESSED" and not force:
            return Submission(existing["id"], url, "PROCESSED", needs_processing=False)

        lead_id = await self._repo.insert_pending_lead(url, extract_domain(url))
        if existing is not None:
            # ``insert_pending_lead`` leaves an existing row untouched, so a
            # FAILED or forced-PROCESSED lead would keep reporting its old
            # outcome while the re-run is in flight and mislead pollers.
            await self._repo.reset_to_pending(lead_id)
        return Submission(lead_id, url, "PENDING", needs_processing=True)

    async def process_lead(self, lead_id: int, url: str) -> Outcome:
        """Run scrape → enrich → persist for an existing lead record.

        Never raises for pipeline failures: any error is logged and recorded on
        the lead as ``'FAILED'``.  This matters for background execution, where
        nobody awaits the result and an escaped exception would leave the lead
        ``'PENDING'`` forever.

        Returns:
            ``"processed"`` on success, ``"failed"`` when the failure was
            recorded, ``"skipped"`` when the lead is already being processed.
        """
        if lead_id in self._in_flight:
            logger.info(
                "Lead already in flight, skipping | url='%s' id=%d", url, lead_id
            )
            return "skipped"
        self._in_flight.add(lead_id)

        try:
            async with self._semaphore:
                return await self._run_pipeline(lead_id, url)
        finally:
            self._in_flight.discard(lead_id)

    async def enrich_url(self, url: str, *, force: bool = False) -> Outcome:
        """Submit and synchronously process *url*; the CLI's per-URL entry point."""
        submission = await self.submit(url, force=force)
        if not submission.needs_processing:
            logger.info(
                "Skipping already-processed URL (use --force to re-enrich): %s", url
            )
            return "skipped"
        return await self.process_lead(submission.lead_id, url)

    async def _run_pipeline(self, lead_id: int, url: str) -> Outcome:
        logger.info("Pipeline start | url='%s' id=%d", url, lead_id)
        try:
            markdown = await self._scraper(url, self._page_timeout_ms)
            payload = await self._llm_client.enrich_lead(url, markdown)
            await self._repo.save_lead_success(lead_id, payload, markdown)
        except (ScrapeError, LLMExtractionError) as exc:
            logger.error("Pipeline failure | url='%s' error='%s'", url, exc)
            # Only the sanitised summary is persisted; the full text stays in logs.
            await self._record_failure(lead_id, exc.public_reason)
            return "failed"
        except Exception as exc:
            # Catch-all by design: see ``process_lead``.  The full traceback is
            # logged; only the exception type is stored, since arbitrary
            # exception text (DSNs, paths) must not leak through the API.
            logger.exception("Pipeline crashed | url='%s' id=%d", url, lead_id)
            await self._record_failure(
                lead_id, f"Unexpected error: {type(exc).__name__}"
            )
            return "failed"

        logger.info(
            "Pipeline success | url='%s' company='%s' score=%d tier='%s'",
            url,
            payload.company_name,
            payload.scoring.fit_score,
            payload.scoring.fit_tier,
        )
        return "processed"

    async def _record_failure(self, lead_id: int, error_message: str) -> None:
        """Best-effort ``mark_lead_failed`` that never masks the original error."""
        try:
            await self._repo.mark_lead_failed(lead_id, error_message)
        except Exception:
            logger.exception("Could not record failure | id=%d", lead_id)
