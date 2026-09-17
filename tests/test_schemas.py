import pytest
from pydantic import ValidationError

from src.llm.schemas import (
    CompanyAnalysis,
    EnrichedLeadPayload,
    LeadScoring,
    OutreachStrategy,
)


def test_enriched_lead_payload_valid():
    payload = EnrichedLeadPayload(
        company_name="TestCorp",
        analysis=CompanyAnalysis(
            industry="B2B SaaS",
            target_audience="SMB",
            value_proposition="Automate B2B sales pipelines easily.",
            pain_points=["Manual data entry", "Low lead conversion", "Slow response time"],
        ),
        scoring=LeadScoring(
            fit_tier="Tier 1 (High)",
            fit_score=90,
            scoring_rationale="Clear B2B SaaS company fitting ideal ICP criteria.",
            missing_information=[],
        ),
        outreach=OutreachStrategy(
            icebreaker="Loved your approach to streamlining pipeline operations.",
            suggested_angle="Direct outreach showing automation integration value.",
        ),
    )

    assert payload.company_name == "TestCorp"
    assert payload.scoring.fit_score == 90
    assert payload.scoring.fit_tier == "Tier 1 (High)"


def test_fit_score_range_validation():
    with pytest.raises(ValidationError):
        LeadScoring(
            fit_tier="Tier 1 (High)",
            fit_score=150,  # Ошибка: должно быть <= 100
            scoring_rationale="Invalid score test.",
            missing_information=[],
        )