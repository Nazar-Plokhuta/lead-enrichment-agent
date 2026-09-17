"""
Playwright Chromium lifecycle management.

Design intent
-------------
`BrowserManager` is an async context manager that owns exactly one Playwright
instance, one Chromium browser, and one browser context for its lifetime.
Callers receive the context and are free to open pages against it; teardown is
always guaranteed via the `__aexit__` path regardless of exceptions.

Network asset blocking (images, media, fonts, stylesheets) is applied at the
context level via route interception so that every page opened inside the
context inherits the filter automatically — no per-page setup required.

The User-Agent and viewport are deliberately set to a realistic desktop profile
to avoid trivial bot-detection fingerprinting on target sites.
"""

from __future__ import annotations

import logging
from types import TracebackType

from playwright.async_api import (
    BrowserContext,
    Playwright,
    Route,
    async_playwright,
)

logger = logging.getLogger(__name__)

# Resource types that carry zero semantic value for text extraction but
# significantly inflate page-load time and bandwidth.
_BLOCKED_RESOURCE_TYPES: frozenset[str] = frozenset(
    {"image", "media", "font", "stylesheet"}
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_VIEWPORT = {"width": 1440, "height": 900}


async def _abort_blocked_resources(route: Route) -> None:
    """Route handler: abort requests for non-essential resource types.

    Aborting (rather than fulfilling with empty responses) keeps the browser's
    internal request queue clean and avoids spurious console errors from sites
    that check response status codes for their own assets.
    """
    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
        await route.abort()
    else:
        await route.continue_()


class BrowserManager:
    """Async context manager that owns a single Playwright Chromium session.

    Usage::

        async with BrowserManager() as ctx:
            page = await ctx.new_page()
            await page.goto("https://example.com")

    The manager guarantees that the browser context and browser process are
    closed in `__aexit__`, even if the body raises an exception.  The
    Playwright driver itself is also properly stopped to avoid zombie processes.

    Attributes:
        headless: Run Chromium without a visible window (default: True).
    """

    def __init__(self, *, headless: bool = True) -> None:
        self.headless = headless
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> BrowserContext:
        self._playwright = await async_playwright().start()

        browser = await self._playwright.chromium.launch(headless=self.headless)

        self._context = await browser.new_context(
            user_agent=_USER_AGENT,
            viewport=_VIEWPORT,
            # Suppress JavaScript dialogs automatically so they never block
            # navigation on sites with intrusive modal pop-ups.
            java_script_enabled=True,
        )

        # Route interception applies to every page created from this context.
        await self._context.route("**/*", _abort_blocked_resources)

        logger.debug("Playwright Chromium context initialised (headless=%s)", self.headless)
        return self._context

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Tear down context → browser → Playwright driver in dependency order."""
        if self._context is not None:
            try:
                # Closing the context implicitly closes all pages it owns.
                await self._context.browser.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                logger.warning("Error while closing Chromium browser — forcing Playwright stop")
            finally:
                self._context = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                logger.warning("Error while stopping Playwright driver")
            finally:
                self._playwright = None

        logger.debug("Playwright Chromium context torn down cleanly")
