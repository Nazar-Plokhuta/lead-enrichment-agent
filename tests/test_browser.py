"""Offline tests for the scraper's request filter, lifecycle and error mapping."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from src.core.exceptions import ScrapeError
from src.core.url_safety import UnsafeUrlError, assert_public_destination
from src.scraper import browser as browser_module
from src.scraper import fetcher as fetcher_module
from src.scraper.browser import (
    BrowserManager,
    _DestinationPolicy,
    _redirect_target,
    _RedirectGuard,
    _RequestFilter,
)

pytestmark = pytest.mark.asyncio

_PUBLIC_IP = "93.184.216.34"


class FakeRoute:
    def __init__(self, url: str, resource_type: str = "document") -> None:
        self.request = SimpleNamespace(url=url, resource_type=resource_type)
        self.actions: list[tuple[str, str | None]] = []

    async def abort(self, error_code: str | None = None) -> None:
        self.actions.append(("abort", error_code))

    async def continue_(self) -> None:
        self.actions.append(("continue", None))


class RecordingGuard:
    """Allows ``example.com``; everything else counts as non-public."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, url: str) -> None:
        self.calls.append(url)
        if "example.com" not in url:
            raise UnsafeUrlError("not public")


def _filter(guard: Any) -> _RequestFilter:
    return _RequestFilter(_DestinationPolicy(guard))


# --- request filter (H1) --------------------------------------------------


async def test_filter_blocks_private_destination() -> None:
    handler = _filter(RecordingGuard())
    route = FakeRoute("http://169.254.169.254/latest/meta-data/")
    await handler(route)  # type: ignore[arg-type]
    assert route.actions == [("abort", "blockedbyclient")]


async def test_filter_continues_public_requests() -> None:
    handler = _filter(RecordingGuard())
    route = FakeRoute("https://www.example.com/")
    await handler(route)  # type: ignore[arg-type]
    assert route.actions == [("continue", None)]


@pytest.mark.parametrize("resource_type", ["image", "media", "font", "stylesheet"])
async def test_filter_aborts_assets_without_consulting_the_guard(
    resource_type: str,
) -> None:
    guard = RecordingGuard()
    route = FakeRoute("https://www.example.com/a.png", resource_type)
    await _filter(guard)(route)  # type: ignore[arg-type]
    assert route.actions == [("abort", None)]
    assert guard.calls == []


async def test_filter_lets_inline_schemes_through_without_a_lookup() -> None:
    guard = RecordingGuard()
    handler = _filter(guard)
    for url in ("data:text/html,hi", "blob:https://example.com/x", "about:blank"):
        route = FakeRoute(url)
        await handler(route)  # type: ignore[arg-type]
        assert route.actions == [("continue", None)]
    assert guard.calls == []


async def test_filter_caches_the_verdict_per_origin() -> None:
    guard = RecordingGuard()
    handler = _filter(guard)
    for path in ("/", "/app.js", "/api?q=1"):
        await handler(FakeRoute(f"https://www.example.com{path}"))  # type: ignore[arg-type]
    await handler(FakeRoute("https://www.example.com:8443/"))  # type: ignore[arg-type]
    assert len(guard.calls) == 2  # default port and :8443 are distinct origins


async def test_filter_fails_closed_when_the_guard_crashes() -> None:
    async def _crash(url: str) -> None:
        raise RuntimeError("resolver exploded")

    route = FakeRoute("https://www.example.com/")
    await _filter(_crash)(route)  # type: ignore[arg-type]
    assert route.actions == [("abort", "blockedbyclient")]


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5:8080/",
        "http://[::1]/",
        "http://localhost/",
        "file:///etc/passwd",
        "ftp://example.com/",
    ],
)
async def test_default_guard_rejects_internal_destinations(url: str) -> None:
    async def _resolver(host: str, port: int) -> list[str]:
        return [_PUBLIC_IP]

    with pytest.raises(UnsafeUrlError):
        await assert_public_destination(url, _resolver)


async def test_default_guard_accepts_long_subresource_urls() -> None:
    async def _resolver(host: str, port: int) -> list[str]:
        return [_PUBLIC_IP]

    # Over the 2048-char cap that applies to submitted URLs, not to sub-requests.
    await assert_public_destination(
        "https://cdn.example.com/x?" + "a" * 4000, _resolver
    )


# --- BrowserManager lifecycle (H3) ----------------------------------------


class FakeBrowser:
    def __init__(
        self, *, context_error: bool = False, close_error: BaseException | None = None
    ) -> None:
        self.closed = False
        self._context_error = context_error
        self._close_error = close_error

    async def new_context(self, **_kwargs: Any) -> Any:
        if self._context_error:
            raise RuntimeError("context failed")

        async def _route(pattern: str, handler: Any) -> None:
            self.route = (pattern, handler)

        return SimpleNamespace(route=_route)

    async def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class FakePlaywright:
    def __init__(
        self, browser: FakeBrowser | None = None, launch_error: bool = False
    ) -> None:
        self.stopped = False
        self._browser = browser or FakeBrowser()

        async def _launch(**_kwargs: Any) -> FakeBrowser:
            if launch_error:
                raise RuntimeError("launch failed")
            return self._browser

        self.chromium = SimpleNamespace(launch=_launch)

    async def stop(self) -> None:
        self.stopped = True


def _install(monkeypatch: pytest.MonkeyPatch, pw: FakePlaywright) -> None:
    async def _start() -> FakePlaywright:
        return pw

    monkeypatch.setattr(
        browser_module, "async_playwright", lambda: SimpleNamespace(start=_start)
    )


async def test_failed_launch_still_stops_the_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pw = FakePlaywright(launch_error=True)
    _install(monkeypatch, pw)

    with pytest.raises(RuntimeError, match="launch failed"):
        async with BrowserManager():
            pytest.fail("body must not run")

    assert pw.stopped


async def test_failed_context_creation_closes_browser_and_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakeBrowser(context_error=True)
    pw = FakePlaywright(browser)
    _install(monkeypatch, pw)

    with pytest.raises(RuntimeError, match="context failed"):
        async with BrowserManager():
            pytest.fail("body must not run")

    assert browser.closed and pw.stopped


async def test_exit_closes_everything_when_the_body_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakeBrowser()
    pw = FakePlaywright(browser)
    _install(monkeypatch, pw)

    with pytest.raises(ValueError, match="body"):
        async with BrowserManager():
            raise ValueError("body")

    assert browser.closed and pw.stopped


async def test_driver_is_stopped_even_if_browser_close_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pw = FakePlaywright(FakeBrowser(close_error=RuntimeError("close failed")))
    _install(monkeypatch, pw)

    async with BrowserManager():
        pass

    assert pw.stopped
    # The failure is logged with its traceback, not swallowed silently.
    assert any(r.exc_info for r in caplog.records if "closing Chromium" in r.message)


async def test_driver_is_stopped_when_cancelled_during_browser_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pw = FakePlaywright(FakeBrowser(close_error=asyncio.CancelledError()))
    _install(monkeypatch, pw)

    with pytest.raises(asyncio.CancelledError):
        async with BrowserManager():
            pass

    assert pw.stopped


async def test_manager_installs_the_request_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakeBrowser()
    _install(monkeypatch, FakePlaywright(browser))

    async with BrowserManager():
        pass

    pattern, handler = browser.route  # type: ignore[attr-defined]
    assert pattern == "**/*"
    assert isinstance(handler, _RequestFilter)


# --- redirect guard (H1) ---------------------------------------------------


def _paused(
    status: int, location: str | None, url: str = "https://example.com/a"
) -> Any:
    headers = [{"name": "Content-Type", "value": "text/html"}]
    if location is not None:
        headers.append({"name": "LOCATION", "value": location})
    return {
        "requestId": "r1",
        "request": {"url": url},
        "responseStatusCode": status,
        "responseHeaders": headers,
    }


class FakeSession:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.handlers: dict[str, Any] = {}
        self.fail_on: str | None = None

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    async def send(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.sent.append((method, params or {}))
        if method == self.fail_on:
            raise RuntimeError("cdp send failed")


@pytest.mark.parametrize(
    ("status", "location", "expected"),
    [
        (302, "http://169.254.169.254/", "http://169.254.169.254/"),
        (301, "/next?x=1", "https://example.com/next?x=1"),
        (307, "//other.example.com/p", "https://other.example.com/p"),
        (200, "http://169.254.169.254/", None),  # not a redirect
        (304, None, None),
        (302, None, None),  # redirect without a target
    ],
)
async def test_redirect_target_extraction(
    status: int, location: str | None, expected: str | None
) -> None:
    assert _redirect_target(_paused(status, location)) == expected


async def _run_guard(
    params: dict[str, Any], session: FakeSession | None = None
) -> FakeSession:
    session = session or FakeSession()
    guard = _RedirectGuard(_DestinationPolicy(RecordingGuard()))
    await guard._handle(session, params)  # type: ignore[arg-type]
    return session


async def test_redirect_to_private_address_is_failed_before_it_is_followed() -> None:
    session = await _run_guard(_paused(302, "http://169.254.169.254/latest/meta-data/"))
    assert session.sent == [
        ("Fetch.failRequest", {"requestId": "r1", "errorReason": "BlockedByClient"})
    ]


async def test_redirect_to_public_address_is_resumed() -> None:
    session = await _run_guard(_paused(302, "https://www.example.com/home"))
    assert session.sent == [("Fetch.continueResponse", {"requestId": "r1"})]


async def test_non_redirect_responses_are_resumed_without_a_lookup() -> None:
    guard = RecordingGuard()
    session = FakeSession()
    await _RedirectGuard(_DestinationPolicy(guard))._handle(  # type: ignore[arg-type]
        session, _paused(200, None)
    )
    assert session.sent == [("Fetch.continueResponse", {"requestId": "r1"})]
    assert guard.calls == []


async def test_redirect_guard_fails_closed_on_internal_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = FakeSession()
    session.fail_on = "Fetch.continueResponse"
    await _run_guard(_paused(302, "https://www.example.com/home"), session)
    assert session.sent[-1][0] == "Fetch.failRequest"
    assert any(r.exc_info for r in caplog.records)  # logged, not swallowed


async def test_redirect_guard_survives_a_closed_target() -> None:
    session = FakeSession()
    session.fail_on = "Fetch.failRequest"
    await _run_guard(_paused(302, "http://10.0.0.1/"), session)  # must not raise


async def test_attach_enables_response_stage_interception() -> None:
    session = FakeSession()

    async def _new_cdp_session(page: Any) -> FakeSession:
        return session

    page = SimpleNamespace(context=SimpleNamespace(new_cdp_session=_new_cdp_session))
    await _RedirectGuard(_DestinationPolicy(RecordingGuard())).attach(page)  # type: ignore[arg-type]

    assert "Fetch.requestPaused" in session.handlers
    assert session.sent == [
        (
            "Fetch.enable",
            {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]},
        )
    ]


async def test_new_page_has_the_redirect_guard_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    page = SimpleNamespace()

    async def _new_cdp_session(_page: Any) -> FakeSession:
        return session

    async def _new_page() -> Any:
        page.context = SimpleNamespace(new_cdp_session=_new_cdp_session)
        return page

    class _Browser(FakeBrowser):
        async def new_context(self, **_kwargs: Any) -> Any:
            ctx = await super().new_context()
            ctx.new_page = _new_page
            return ctx

    _install(monkeypatch, FakePlaywright(_Browser()))

    async with BrowserManager() as browser:
        assert await browser.new_page() is page

    assert [m for m, _ in session.sent] == ["Fetch.enable"]


async def test_new_page_closes_the_page_if_the_guard_cannot_attach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []

    async def _new_cdp_session(_page: Any) -> Any:
        raise RuntimeError("attach failed")

    async def _close() -> None:
        closed.append(True)

    async def _new_page() -> Any:
        return SimpleNamespace(
            context=SimpleNamespace(new_cdp_session=_new_cdp_session), close=_close
        )

    class _Browser(FakeBrowser):
        async def new_context(self, **_kwargs: Any) -> Any:
            ctx = await super().new_context()
            ctx.new_page = _new_page
            return ctx

    _install(monkeypatch, FakePlaywright(_Browser()))

    async with BrowserManager() as browser:
        with pytest.raises(RuntimeError, match="attach failed"):
            await browser.new_page()

    assert closed == [True]


async def test_new_page_requires_an_entered_manager() -> None:
    with pytest.raises(RuntimeError, match="entered"):
        await BrowserManager().new_page()


# --- fetcher error sanitisation (H7) --------------------------------------


class _FakePage:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def goto(self, *_args: Any, **_kwargs: Any) -> None:
        raise self._error

    async def close(self) -> None:
        pass


class _FakeManager:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def __aenter__(self) -> Any:
        async def _new_page() -> _FakePage:
            return _FakePage(self._error)

        return SimpleNamespace(new_page=_new_page)

    async def __aexit__(self, *_exc: object) -> None:
        return None


async def _fetch_error(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> ScrapeError:
    monkeypatch.setattr(fetcher_module, "BrowserManager", lambda: _FakeManager(error))
    with pytest.raises(ScrapeError) as info:
        await fetcher_module.fetch_page_markdown("https://example.com", 1000)
    return info.value


async def test_policy_block_is_reported_without_the_target_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err = await _fetch_error(
        monkeypatch, PlaywrightError("net::ERR_BLOCKED_BY_CLIENT at http://10.0.0.5/")
    )
    assert "10.0.0.5" in err.reason  # kept for the logs
    assert err.public_reason == "URL blocked: destination is not a public address"
    assert "10.0.0.5" not in err.public_reason


async def test_other_navigation_errors_expose_no_driver_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err = await _fetch_error(
        monkeypatch,
        PlaywrightError("net::ERR_CONNECTION_REFUSED at http://10.0.0.5:8080/"),
    )
    assert err.public_reason == "Page could not be loaded"
