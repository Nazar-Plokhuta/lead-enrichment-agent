"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.routes import router
from src.config import Settings, get_repository, get_settings
from src.core.log_config import configure_logging
from src.llm.client import LLMClient
from src.scraper.fetcher import fetch_page_markdown
from src.services.enrichment import EnrichmentService, Scraper

logger = logging.getLogger(__name__)

API_VERSION = "2.0.0"


def create_app(
    settings: Settings | None = None,
    *,
    llm_client: LLMClient | None = None,
    scraper: Scraper = fetch_page_markdown,
) -> FastAPI:
    """Build the application.

    Args:
        settings:   Defaults to the process-wide settings singleton.
        llm_client: Defaults to a real ``LLMClient``; tests inject a fake.
        scraper:    Defaults to the Playwright fetcher; tests inject a fake.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or get_settings()
        configure_logging(cfg.LOG_LEVEL)
        repo = get_repository(cfg)
        # Schema (and, for Postgres, the pool) must exist before any request.
        await repo.init_db()
        try:
            app.state.repo = repo
            app.state.service = EnrichmentService(
                repo,
                llm_client or LLMClient(cfg),
                asyncio.Semaphore(cfg.MAX_CONCURRENT_SCRAPES),
                page_timeout_ms=cfg.PAGE_TIMEOUT_MS,
                scraper=scraper,
            )
            logger.info("API started | version=%s", API_VERSION)
            yield
        finally:
            await repo.close()
            logger.info("API stopped")

    app = FastAPI(
        title="Lead Enrichment Agent API",
        version=API_VERSION,
        description="Asynchronous B2B lead enrichment and ICP scoring.",
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
