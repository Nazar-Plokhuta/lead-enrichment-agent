"""
Playwright Chromium lifecycle management.

Design intent
-------------
`BrowserManager` is an async context manager that owns exactly one Playwright
instance, one Chromium browser, and one browser context for its lifetime.
Callers open pages through `BrowserManager.new_page`; teardown is always
guaranteed via the `__aexit__` path regardless of exceptions.

Network asset blocking (images, media, fonts, stylesheets) is applied at the
context level via route interception so that every page opened inside the
context inherits the filter automatically — no per-page setup required.

SSRF policy is enforced at two layers, because neither alone is enough:

* The route handler vets every request Chromium *initiates* (navigation and
  sub-resources), before it is sent.
* Playwright's route handler does **not** see redirect hops: Chromium follows
  a 3xx internally and Playwright only reports it afterwards as a passive
  ``request`` event.  A public URL that 302s to ``169.254.169.254`` would sail
  through, so each page also gets a CDP ``Fetch`` interceptor at the *response*
  stage that reads the ``Location`` header of every redirect and fails the
  request before Chromium follows it.

Remaining limits: Chromium re-resolves names when it connects (DNS rebinding),
and popups/cross-site iframes run in targets without the redirect interceptor.
Egress filtering on the host is still the backstop.

The User-Agent and viewport are deliberately set to a realistic desktop profile
to avoid trivial bot-detection fingerprinting on target sites.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, Self
from urllib.parse import urljoin, urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    CDPSession,
    Page,
    Playwright,
    Route,
    async_playwright,
)

from src.core.url_safety import UnsafeUrlError, assert_public_destination

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

# Pages may embed these without a network round-trip, so there is no host to vet.
_LOCAL_SCHEMES = ("data:", "blob:", "about:")

# Raises ``UnsafeUrlError`` for a request that must not be sent.
UrlGuard = Callable[[str], Awaitable[None]]


class _DestinationPolicy:
    """Cached allow/deny verdicts for request destinations.

    Verdicts are cached per ``scheme://host:port`` for the lifetime of the
    context so a page issuing hundreds of same-origin requests costs one DNS
    lookup, not hundreds.  The policy fails closed: a failed lookup or a
    crashing guard denies the request.
    """

    def __init__(self, guard: UrlGuard) -> None:
        self._guard = guard
        self._verdicts: dict[str, bool] = {}

    async def allows(self, url: str) -> bool:
        if url.lower().startswith(_LOCAL_SCHEMES):
            return True

        origin = _origin_key(url)
        cached = self._verdicts.get(origin)
        if cached is not None:
            return cached

        try:
            await self._guard(url)
        except UnsafeUrlError as exc:
            logger.warning("Blocked browser request to non-public destination: %s", exc)
            allowed = False
        except Exception:
            # An unexpected guard failure must still block the request; leaving
            # it unanswered would hang the navigation instead.
            logger.exception("URL guard crashed; blocking request")
            allowed = False
        else:
            allowed = True
        self._verdicts[origin] = allowed
        return allowed


class _RequestFilter:
    """Route handler: drop non-essential assets and non-public destinations.

    Unsafe requests are *aborted* (not fulfilled) so the page sees a network
    error rather than attacker-shaped content.
    """

    def __init__(self, policy: _DestinationPolicy) -> None:
        self._policy = policy

    async def __call__(self, route: Route) -> None:
        request = route.request
        if request.resource_type in _BLOCKED_RESOURCE_TYPES:
            await route.abort()
        elif await self._policy.allows(request.url):
            await route.continue_()
        else:
            await route.abort("blockedbyclient")


class _RedirectGuard:
    """CDP interceptor that vets redirect targets before Chromium follows them.

    Every response is paused at the ``Response`` stage; only 3xx responses with
    a ``Location`` are inspected, everything else is resumed untouched.  The
    handler must always answer a paused request or the page would hang, so any
    internal error fails the request instead.
    """

    def __init__(self, policy: _DestinationPolicy) -> None:
        self._policy = policy
        # Strong references: the event loop only keeps weak ones to tasks.
        self._tasks: set[asyncio.Task[None]] = set()

    async def attach(self, page: Page) -> None:
        session = await page.context.new_cdp_session(page)
        session.on("Fetch.requestPaused", lambda params: self._spawn(session, params))
        await session.send(
            "Fetch.enable",
            {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]},
        )

    def _spawn(self, session: CDPSession, params: dict[str, Any]) -> None:
        task = asyncio.create_task(self._handle(session, params))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, session: CDPSession, params: dict[str, Any]) -> None:
        request_id = params["requestId"]
        try:
            target = _redirect_target(params)
            if target is not None and not await self._policy.allows(target):
                await session.send(
                    "Fetch.failRequest",
                    {"requestId": request_id, "errorReason": "BlockedByClient"},
                )
            else:
                await session.send("Fetch.continueResponse", {"requestId": request_id})
        except Exception:
            logger.warning("Redirect guard failed; failing request", exc_info=True)
            try:
                await session.send(
                    "Fetch.failRequest",
                    {"requestId": request_id, "errorReason": "BlockedByClient"},
                )
            except Exception:
                # The page or browser is already gone; nothing is left to hang.
                logger.debug("Could not fail paused request", exc_info=True)


def _redirect_target(params: dict[str, Any]) -> str | None:
    """Return the absolute ``Location`` of a paused 3xx response, else ``None``."""
    if not 300 <= params.get("responseStatusCode", 0) < 400:
        return None
    for header in params.get("responseHeaders", []):
        if header["name"].lower() == "location":
            return urljoin(params["request"]["url"], header["value"])
    return None


def _origin_key(url: str) -> str:
    """Return ``scheme://host:port`` for the verdict cache (userinfo dropped)."""
    try:
        parts = urlsplit(url)
        return f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}:{parts.port}"
    except ValueError:
        return url  # malformed: the guard rejects it, and it is cached as-is


class BrowserManager:
    """Async context manager that owns a single Playwright Chromium session.

    Usage::

        async with BrowserManager() as browser:
            page = await browser.new_page()
            await page.goto("https://example.com")

    The manager guarantees that the browser context and browser process are
    closed in `__aexit__`, even if the body raises an exception.  The
    Playwright driver itself is also properly stopped to avoid zombie processes.

    Attributes:
        headless: Run Chromium without a visible window (default: True).
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        url_guard: UrlGuard = assert_public_destination,
    ) -> None:
        self.headless = headless
        self._policy = _DestinationPolicy(url_guard)
        self._redirect_guard = _RedirectGuard(self._policy)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> Self:
        # ``__aexit__`` never runs when ``__aenter__`` raises, so a failed
        # launch must release the driver subprocess itself.  BaseException so
        # that cancellation mid-launch cleans up as well.
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless
            )
            self._context = await self._browser.new_context(
                user_agent=_USER_AGENT,
                viewport=_VIEWPORT,
                java_script_enabled=True,
            )
            # Route interception applies to every page created from this context.
            await self._context.route("**/*", _RequestFilter(self._policy))
        except BaseException:
            await self._teardown()
            raise

        logger.debug(
            "Playwright Chromium context initialised (headless=%s)", self.headless
        )
        return self

    async def new_page(self) -> Page:
        """Open a page with request filtering and redirect vetting in force.

        Pages are only created here so none can exist without the redirect
        guard (attached before the caller can navigate).
        """
        if self._context is None:
            raise RuntimeError("BrowserManager must be entered before use")
        page = await self._context.new_page()
        try:
            await self._redirect_guard.attach(page)
        except BaseException:
            await page.close()
            raise
        return page

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self._teardown()

    async def _teardown(self) -> None:
        """Close browser → stop Playwright driver, whatever has been started.

        Each step is isolated so a failure (or cancellation) while closing the
        browser still stops the driver, which is the process that would
        otherwise outlive us.  Closing the browser also closes its contexts
        and pages.
        """
        browser, playwright = self._browser, self._playwright
        self._context = self._browser = self._playwright = None

        try:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    logger.warning(
                        "Error while closing Chromium browser", exc_info=True
                    )
        finally:
            if playwright is not None:
                try:
                    await playwright.stop()
                except Exception:
                    logger.warning(
                        "Error while stopping Playwright driver", exc_info=True
                    )

        logger.debug("Playwright Chromium context torn down")
