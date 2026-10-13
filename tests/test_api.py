"""Offline tests for the REST API.

The app runs against a throwaway SQLite file with a fake scraper, a fake LLM
client and a fake DNS resolver, so no browser, provider or network is touched.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from src.api.app import create_app
from src.api.routes import get_host_resolver
from src.api.security import UnsafeUrlError, assert_public_host, validate_url_syntax
from src.config import Settings
from src.core.exceptions import LLMExtractionError, ScrapeError
from src.llm.schemas import (
    CompanyAnalysis,
    EnrichedLeadPayload,
    LeadScoring,
    OutreachStrategy,
)

pytestmark = pytest.mark.asyncio

_PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def _reject_outbound_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything opens a connection or performs a DNS lookup."""

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("API test attempted a network operation")

    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)


def _payload(
    company: str = "Acme", score: int = 80, tier: str = "Tier 1 (High)"
) -> Any:
    return EnrichedLeadPayload(
        company_name=company,
        analysis=CompanyAnalysis(
            industry="B2B SaaS",
            target_audience="SMB",
            value_proposition="Automates pipelines.",
            pain_points=["Manual entry"],
        ),
        scoring=LeadScoring(
            fit_tier=tier,  # type: ignore[arg-type]
            fit_score=score,
            scoring_rationale="Clear B2B fit.",
        ),
        outreach=OutreachStrategy(icebreaker="Hi.", suggested_angle="Automation."),
    )


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def enrich_lead(self, url: str, markdown_content: str) -> EnrichedLeadPayload:
        self.calls.append(url)
        return _payload(company=f"Co {url}")


class FakeScraper:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error: Exception | None = None

    async def __call__(self, url: str, timeout_ms: int) -> str:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return "# Acme\nB2B SaaS platform."


@pytest.fixture
def scraper() -> FakeScraper:
    return FakeScraper()


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest_asyncio.fixture
async def app(
    tmp_path: Path, scraper: FakeScraper, llm: FakeLLM
) -> AsyncIterator[FastAPI]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        DATABASE_PATH=str(tmp_path / "leads.db"),
        DATABASE_URL=None,
    )
    application = create_app(settings, llm_client=llm, scraper=scraper)  # type: ignore[arg-type]

    async def _resolver(host: str, port: int) -> list[str]:
        return [_PUBLIC_IP]

    application.dependency_overrides[get_host_resolver] = lambda: _resolver
    # ASGITransport does not drive the lifespan, so run it explicitly.
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c


# --- health ---------------------------------------------------------------


async def test_health_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy", "database": "ok"}


async def test_health_reports_503_when_database_fails(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    async def _boom() -> bool:
        raise ConnectionError("db down")

    app.state.repo.ping = _boom
    resp = await client.get("/health")
    assert resp.status_code == 503
    assert resp.json() == {"status": "unhealthy", "database": "unreachable"}


# --- POST /enrich ---------------------------------------------------------


async def test_enrich_returns_202_and_pipeline_completes(
    client: httpx.AsyncClient, scraper: FakeScraper, llm: FakeLLM
) -> None:
    resp = await client.post("/api/v1/enrich", json={"url": "https://linear.app"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["url"] == "https://linear.app"  # not rewritten to a trailing slash
    assert body["status"] == "PENDING"

    # The ASGI transport runs background tasks before returning.
    lead = (await client.get(f"/api/v1/leads/{body['lead_id']}")).json()
    assert lead["status"] == "PROCESSED"
    assert lead["company_name"] == "Co https://linear.app"
    assert lead["fit_score"] == 80
    assert lead["enriched_data"]["scoring"]["fit_tier"] == "Tier 1 (High)"
    assert scraper.calls == llm.calls == ["https://linear.app"]


async def test_enrich_already_processed_is_not_rerun_unless_forced(
    client: httpx.AsyncClient, scraper: FakeScraper
) -> None:
    payload = {"url": "https://linear.app"}
    first = (await client.post("/api/v1/enrich", json=payload)).json()

    again = await client.post("/api/v1/enrich", json=payload)
    assert again.status_code == 200
    assert again.json()["lead_id"] == first["lead_id"]
    assert again.json()["status"] == "PROCESSED"
    assert len(scraper.calls) == 1

    forced = await client.post("/api/v1/enrich", json={**payload, "force": True})
    assert forced.status_code == 202
    assert len(scraper.calls) == 2


async def test_scrape_failure_is_recorded_without_driver_detail(
    client: httpx.AsyncClient, scraper: FakeScraper
) -> None:
    scraper.error = ScrapeError(
        "https://down.example",
        "net::ERR_CONNECTION_REFUSED at http://10.0.0.5:8080",
        public_reason="Page could not be loaded",
    )
    resp = await client.post("/api/v1/enrich", json={"url": "https://down.example"})
    assert resp.status_code == 202

    lead = (await client.get(f"/api/v1/leads/{resp.json()['lead_id']}")).json()
    assert lead["status"] == "FAILED"
    assert lead["error_message"] == "Page could not be loaded"
    assert "10.0.0.5" not in str(lead)


async def test_llm_failure_is_recorded_without_vendor_detail(
    client: httpx.AsyncClient, llm: FakeLLM
) -> None:
    async def _fail(url: str, markdown_content: str) -> EnrichedLeadPayload:
        raise LLMExtractionError(
            url,
            "Incorrect API key provided: sk-proj-****abcd at https://api.openai.com",
            public_reason="LLM provider request failed (AuthenticationError)",
        )

    llm.enrich_lead = _fail  # type: ignore[method-assign]
    resp = await client.post("/api/v1/enrich", json={"url": "https://llm-fail.example"})

    lead = (await client.get(f"/api/v1/leads/{resp.json()['lead_id']}")).json()
    assert lead["status"] == "FAILED"
    assert lead["error_message"] == "LLM provider request failed (AuthenticationError)"
    assert "sk-proj" not in str(lead)


async def test_unexpected_pipeline_error_marks_lead_failed(
    client: httpx.AsyncClient, scraper: FakeScraper
) -> None:
    scraper.error = RuntimeError("secret internal detail")
    resp = await client.post("/api/v1/enrich", json={"url": "https://boom.example"})
    assert resp.status_code == 202

    lead = (await client.get(f"/api/v1/leads/{resp.json()['lead_id']}")).json()
    assert lead["status"] == "FAILED"
    assert lead["error_message"] == "Unexpected error: RuntimeError"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://localhost:8000",
        "http://LOCALHOST./",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5",
        "http://172.16.0.1",
        "http://192.168.1.1",
        "http://100.64.0.1",
        "http://0.0.0.0",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fe80::1]/",
        "http://metadata.google.internal/",
        "http://printer.local/",
        "http://127.0.0.1\\@example.com/",
        "ftp://example.com",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "example.com",
        "",
    ],
)
async def test_enrich_rejects_unsafe_urls(client: httpx.AsyncClient, url: str) -> None:
    resp = await client.post("/api/v1/enrich", json={"url": url})
    assert resp.status_code == 422
    assert "detail" in resp.json()


async def test_enrich_rejects_hostname_resolving_to_private_address(
    client: httpx.AsyncClient, app: FastAPI, scraper: FakeScraper
) -> None:
    async def _internal(host: str, port: int) -> list[str]:
        return [_PUBLIC_IP, "10.1.2.3"]  # one bad record is enough

    app.dependency_overrides[get_host_resolver] = lambda: _internal
    resp = await client.post("/api/v1/enrich", json={"url": "https://rebind.example"})
    assert resp.status_code == 422
    assert "private or reserved" in resp.json()["detail"]
    assert scraper.calls == []


async def test_enrich_rejects_unresolvable_hostname(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    async def _nxdomain(host: str, port: int) -> list[str]:
        raise socket.gaierror("no such host")

    app.dependency_overrides[get_host_resolver] = lambda: _nxdomain
    resp = await client.post("/api/v1/enrich", json={"url": "https://nope.example"})
    assert resp.status_code == 422
    assert "could not be resolved" in resp.json()["detail"]


# --- GET /leads -----------------------------------------------------------


async def test_get_lead_404_for_unknown_id(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/leads/9999")
    assert resp.status_code == 404


async def test_get_lead_200_for_existing(client: httpx.AsyncClient) -> None:
    lead_id = (
        await client.post("/api/v1/enrich", json={"url": "https://a.example"})
    ).json()["lead_id"]
    resp = await client.get(f"/api/v1/leads/{lead_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == lead_id
    assert body["url"] == "https://a.example"
    assert "raw_markdown" not in body


async def test_list_leads_paginates_and_filters(
    client: httpx.AsyncClient, app: FastAPI, scraper: FakeScraper
) -> None:
    for name in ("a", "b", "c"):
        await client.post("/api/v1/enrich", json={"url": f"https://{name}.example"})
    scraper.error = ScrapeError("https://d.example", "boom")
    await client.post("/api/v1/enrich", json={"url": "https://d.example"})

    page1 = (
        await client.get("/api/v1/leads", params={"page": 1, "page_size": 3})
    ).json()
    assert page1["total"] == 4
    assert (page1["page"], page1["page_size"]) == (1, 3)
    assert [i["url"] for i in page1["items"]] == [
        "https://d.example",
        "https://c.example",
        "https://b.example",
    ]
    page2 = (
        await client.get("/api/v1/leads", params={"page": 2, "page_size": 3})
    ).json()
    assert [i["url"] for i in page2["items"]] == ["https://a.example"]

    failed = (await client.get("/api/v1/leads", params={"status": "FAILED"})).json()
    assert failed["total"] == 1 and failed["items"][0]["url"] == "https://d.example"

    tier = (
        await client.get("/api/v1/leads", params={"fit_tier": "Tier 1 (High)"})
    ).json()
    assert tier["total"] == 3
    none = (
        await client.get("/api/v1/leads", params={"fit_tier": "Disqualified"})
    ).json()
    assert none == {"items": [], "total": 0, "page": 1, "page_size": 50}


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page_size": 0},
        {"page_size": 101},
        {"status": "DONE"},
        {"fit_tier": "x"},
    ],
)
async def test_list_leads_rejects_invalid_query(
    client: httpx.AsyncClient, params: dict[str, Any]
) -> None:
    assert (await client.get("/api/v1/leads", params=params)).status_code == 422


# --- security unit tests --------------------------------------------------


@pytest.mark.parametrize(
    "url", ["https://linear.app", "http://example.com:8080/a?b=1", " https://x.io "]
)
async def test_validate_url_syntax_accepts_public_urls(url: str) -> None:
    assert validate_url_syntax(url) == url.strip()


async def test_assert_public_host_skips_lookup_for_ip_literals() -> None:
    async def _never(host: str, port: int) -> list[str]:
        raise AssertionError("resolver must not be called for IP literals")

    await assert_public_host("http://93.184.216.34/", _never)


async def test_assert_public_host_rejects_obfuscated_loopback() -> None:
    async def _resolve(host: str, port: int) -> list[str]:
        return ["127.0.0.1"]  # what getaddrinfo returns for "2130706433"

    with pytest.raises(UnsafeUrlError):
        await assert_public_host("http://2130706433/", _resolve)
