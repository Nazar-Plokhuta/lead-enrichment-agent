"""
End-to-end page fetch orchestration: URL → clean Markdown.

Design intent
-------------
`fetch_page_markdown` is the single public entry point for the scraping
pipeline.  It wires together the two lower-level modules:

1. `BrowserManager` — owns the Playwright process and a pre-configured
   Chromium context with asset blocking already active.
2. `extract_markdown` — strips DOM boilerplate and returns a token-budget-safe
   Markdown string.

Error handling strategy
-----------------------
Playwright surfaces navigation failures through two distinct exception types:

- `TimeoutError`: the page did not reach the requested load state within the
  allotted `timeout_ms`.  Common on slow or JavaScript-heavy sites.
- `Error` (generic Playwright error): covers connection resets, SSL failures,
  invalid URLs, and renderer crashes.

Both are caught, logged with the failing URL for observability, and re-raised
as `ScrapeError` — the single error type callers outside this package need to
handle.  Using a domain exception keeps the orchestration layer decoupled from
the Playwright API surface.

Wait strategy
-------------
`domcontentloaded` is intentionally preferred over `networkidle` because
`networkidle` waits for *all* network activity to cease, which can hang
indefinitely on pages that poll analytics endpoints or stream server-sent
events.  The asset-blocking route filter in `BrowserManager` further reduces
the window of inflight requests so `domcontentloaded` fires reliably.
"""

from __future__ import annotations

import logging

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.core.exceptions import ScrapeError
from src.scraper.browser import BrowserManager
from src.scraper.cleaner import extract_markdown

logger = logging.getLogger(__name__)


async def fetch_page_markdown(url: str, timeout_ms: int = 20_000) -> str:
    """Navigate to `url` with a headless browser and return clean Markdown.

    The function opens a fresh Playwright page, navigates to the target URL,
    extracts the rendered HTML, and pipes it through `extract_markdown` before
    returning.  The browser context is torn down on exit regardless of outcome.

    Args:
        url: Fully-qualified URL to scrape (e.g. ``"https://example.com"``).
        timeout_ms: Maximum milliseconds to wait for `domcontentloaded` before
            raising `ScrapeError`.  Defaults to 20,000 ms (20 s).

    Returns:
        Clean Markdown string representing the page's primary content.  May be
        an empty string for JavaScript-only shells that produce no static DOM.

    Raises:
        ScrapeError: On Playwright timeout, navigation failure, or any other
            error that prevents retrieving usable page content.
    """
    logger.info("Fetching page: %s (timeout=%dms)", url, timeout_ms)

    async with BrowserManager() as context:
        page = await context.new_page()

        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            logger.warning("Timeout after %dms navigating to %s", timeout_ms, url)
            raise ScrapeError(url=url, reason=f"Navigation timed out after {timeout_ms}ms") from exc
        except PlaywrightError as exc:
            logger.warning("Playwright navigation error for %s: %s", url, exc.message)
            raise ScrapeError(url=url, reason=exc.message) from exc

        try:
            html: str = await page.content()
        except PlaywrightError as exc:
            # `page.content()` can fail if the renderer crashed after navigation.
            logger.warning("Failed to retrieve DOM content for %s: %s", url, exc.message)
            raise ScrapeError(url=url, reason=f"DOM content retrieval failed: {exc.message}") from exc
        finally:
            # Close the page explicitly before the context exits so that any
            # lingering resource handles are freed immediately.
            await page.close()

    markdown = extract_markdown(html)
    logger.info(
        "Extracted %d characters of Markdown from %s",
        len(markdown),
        url,
    )
    return markdown
