"""Resubmission semantics of ``EnrichmentService.submit`` against real SQLite."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from src.llm.schemas import (
    CompanyAnalysis,
    EnrichedLeadPayload,
    LeadScoring,
    OutreachStrategy,
)
from src.services.enrichment import EnrichmentService
from src.storage.repository import LeadRepository

pytestmark = pytest.mark.asyncio

_URL = "https://linear.app"


def _payload() -> EnrichedLeadPayload:
    return EnrichedLeadPayload(
        company_name="Linear",
        analysis=CompanyAnalysis(
            industry="B2B SaaS",
            target_audience="Engineering teams",
            value_proposition="Issue tracking.",
            pain_points=["Slow tools"],
        ),
        scoring=LeadScoring(
            fit_tier="Tier 1 (High)", fit_score=90, scoring_rationale="Strong fit."
        ),
        outreach=OutreachStrategy(icebreaker="Hi.", suggested_angle="Speed."),
    )


@pytest_asyncio.fixture
async def repo(tmp_path: Path) -> AsyncIterator[LeadRepository]:
    repository = LeadRepository(str(tmp_path / "leads.db"))
    await repository.init_db()
    yield repository


@pytest.fixture
def service(repo: LeadRepository) -> EnrichmentService:
    async def _scraper(url: str, timeout_ms: int) -> str:
        return "# page"

    return EnrichmentService(
        repo,
        llm_client=None,  # type: ignore[arg-type]  # submit() never touches the LLM
        semaphore=asyncio.Semaphore(1),
        page_timeout_ms=1000,
        scraper=_scraper,
    )


async def test_resubmitting_failed_lead_clears_stale_failure(
    repo: LeadRepository, service: EnrichmentService
) -> None:
    lead_id = await repo.insert_pending_lead(_URL, "linear.app")
    await repo.mark_lead_failed(lead_id, "Page could not be loaded")

    submission = await service.submit(_URL)

    assert submission.needs_processing and submission.lead_id == lead_id
    row = await repo.get_lead_by_id(lead_id)
    assert row is not None
    assert row["status"] == "PENDING"
    assert row["error_message"] is None


async def test_forced_resubmission_of_processed_lead_goes_back_to_pending(
    repo: LeadRepository, service: EnrichmentService
) -> None:
    lead_id = await repo.insert_pending_lead(_URL, "linear.app")
    await repo.save_lead_success(lead_id, _payload(), "# page")

    assert (await service.submit(_URL)).needs_processing is False
    row = await repo.get_lead_by_id(lead_id)
    assert row is not None and row["status"] == "PROCESSED"

    await service.submit(_URL, force=True)

    row = await repo.get_lead_by_id(lead_id)
    assert row is not None
    assert row["status"] == "PENDING"
    # The previous result stays readable until the re-run overwrites it.
    assert row["company_name"] == "Linear"
    assert row["fit_score"] == 90


async def test_new_url_is_created_pending(
    repo: LeadRepository, service: EnrichmentService
) -> None:
    submission = await service.submit(_URL)

    row = await repo.get_lead_by_id(submission.lead_id)
    assert row is not None and row["status"] == "PENDING"
