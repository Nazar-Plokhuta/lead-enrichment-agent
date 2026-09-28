"""Offline regression suite for the ICP scoring rubric.

Golden pages in ``tests/fixtures/eval`` stand in for scraped Markdown.
Each case carries an expected ``EnrichedLeadPayload`` label. The suite
checks schema acceptance, tier/score bands, and evidence grounding locally.
It never calls OpenAI.
"""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from src.llm.prompts import SYSTEM_PROMPT_TEMPLATE
from src.llm.schemas import (
    CompanyAnalysis,
    EnrichedLeadPayload,
    LeadScoring,
    OutreachStrategy,
)

FitTier = Literal["Tier 1 (High)", "Tier 2 (Medium)", "Tier 3 (Low)", "Disqualified"]

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "eval"

# Inclusive bands from the v2.0 FIT TIER MAPPING section.
_TIER_BOUNDS: dict[FitTier, tuple[int, int]] = {
    "Tier 1 (High)": (75, 100),
    "Tier 2 (Medium)": (50, 74),
    "Tier 3 (Low)": (25, 49),
    "Disqualified": (0, 24),
}

# Dimension ceilings: A business model, B market, C commercial clarity, D proof.
_DIMENSION_CAPS: tuple[int, int, int, int] = (35, 25, 20, 20)

# Chain-of-thought line the rubric requires before fit_score is assigned.
_SCORE_SUMMARY = re.compile(
    r"A=(?P<a>\d+), B=(?P<b>\d+), C=(?P<c>\d+), D=(?P<d>\d+) \u2192 total=(?P<total>\d+)"
)

# Prompt publishes bands with an en dash (U+2013). Spacing around it varies.
_PROMPT_BAND = re.compile(
    r"(Tier 1 \(High\)|Tier 2 \(Medium\)|Tier 3 \(Low\)|Disqualified)"
    r"\s+(\d+)\s+\u2013\s+(\d+)"
)


def tier_for_score(fit_score: int) -> FitTier:
    """Return the rubric tier whose inclusive band contains ``fit_score``."""
    for tier, (lower, upper) in _TIER_BOUNDS.items():
        if lower <= fit_score <= upper:
            return tier
    msg = f"fit_score {fit_score} is outside the inclusive rubric range 0-100"
    raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ScoringEvalCase:
    """One scraped page plus the enrichment label the rubric must accept."""

    fixture_name: str
    company_name: str
    industry: str
    target_audience: str
    value_proposition: str
    pain_points: tuple[str, ...]
    fit_tier: FitTier
    fit_score: int
    dimension_scores: tuple[int, int, int, int]
    business_model_band: tuple[int, int]
    scoring_rationale: str
    missing_information: tuple[str, ...]
    icebreaker: str
    suggested_angle: str
    cited_detail: str
    evidence_anchors: tuple[str, ...]

    def load_page(self) -> str:
        """Read the golden page this case is scored against."""
        return (_FIXTURE_DIR / self.fixture_name).read_text(encoding="utf-8")

    def to_payload(self) -> EnrichedLeadPayload:
        """Validate the golden label through the structured-output DTO.

        ``model_validate`` is the same contract the OpenAI parse path uses,
        exercised here on a local dict so the suite stays offline.
        """
        return EnrichedLeadPayload.model_validate(
            {
                "company_name": self.company_name,
                "analysis": {
                    "industry": self.industry,
                    "target_audience": self.target_audience,
                    "value_proposition": self.value_proposition,
                    "pain_points": list(self.pain_points),
                },
                "scoring": {
                    "fit_tier": self.fit_tier,
                    "fit_score": self.fit_score,
                    "scoring_rationale": self.scoring_rationale,
                    "missing_information": list(self.missing_information),
                },
                "outreach": {
                    "icebreaker": self.icebreaker,
                    "suggested_angle": self.suggested_angle,
                },
            }
        )


@dataclass(frozen=True, slots=True)
class RejectedScoringInput:
    """A ``LeadScoring`` body the schema must refuse."""

    case_id: str
    fit_tier: str
    fit_score: int
    error_loc: tuple[str, ...]
    error_type: str


_EVAL_CASES: tuple[ScoringEvalCase, ...] = (
    ScoringEvalCase(
        fixture_name="tier1_saas.md",
        company_name="Forgeboard",
        industry="B2B SaaS – Engineering Workflow",
        target_audience="Mid-market engineering teams (40–400 employees, Series A–C)",
        value_proposition=(
            "Forgeboard is a per-seat B2B SaaS workspace where engineering teams "
            "plan work, coordinate releases, and review incidents."
        ),
        pain_points=(
            "Status updates live in five tools and nobody trusts the latest version.",
            "Release checklists fall out of date the moment an incident starts.",
            "Cross-team dependencies stay invisible until a deadline slips.",
        ),
        fit_tier="Tier 1 (High)",
        fit_score=92,
        dimension_scores=(33, 23, 18, 18),
        business_model_band=(30, 35),
        scoring_rationale=(
            "Forgeboard is a per-seat B2B SaaS workspace for engineering teams, "
            "with Starter, Growth, and Business plans. The page addresses "
            "mid-market engineering organizations of 40 to 400 employees from "
            "Series A through Series C. Self-serve signup and published per-seat "
            "prices make the commercial motion explicit. Named customers "
            "(Northwind Analytics, Helio Payments, Lumenstack), a public REST API, "
            "GitHub and PagerDuty integrations, and SOC 2 Type II compliance are "
            "present. Exact headcount inside that range is not stated. "
            "A=33, B=23, C=18, D=18 → total=92"
        ),
        missing_information=(
            "Exact employee count is published only as a 40 to 400 range",
        ),
        icebreaker=(
            "Helio Payments rolling the workspace out to 180 engineers is a "
            "specific Forgeboard proof point, alongside SOC 2 Type II compliance."
        ),
        suggested_angle=(
            "Connect outreach to release coordination for mid-market engineering "
            "teams, using the 37% reduction Northwind Analytics reported as the "
            "verified hook."
        ),
        cited_detail="Helio Payments",
        evidence_anchors=(
            "SOC 2 Type II",
            "per seat",
            "GitHub",
            "public REST API",
            "Series A through Series C",
        ),
    ),
    ScoringEvalCase(
        fixture_name="tier2_consulting.md",
        company_name="Harborline Partners",
        industry="Tech-enabled B2B consulting",
        target_audience=(
            "Mixed B2B buyers: founder-led directors and business-unit sponsors, "
            "employee count unstated"
        ),
        value_proposition=(
            "Harborline Partners designs and ships internal tools for B2B software "
            "companies, blending senior consultants with a small engineering pod."
        ),
        pain_points=(
            "Internal tools are scoped in slides and never shipped.",
            "CRM and operations data stay disconnected across the engagement.",
            "Onboarding handoffs depend on a single consultant leaving the project.",
        ),
        fit_tier="Tier 2 (Medium)",
        fit_score=54,
        dimension_scores=(22, 12, 10, 10),
        business_model_band=(15, 25),
        scoring_rationale=(
            "Harborline Partners is a hybrid consultancy that ships working "
            "TypeScript and Postgres applications, so technology is core to "
            "delivery, but the offer is a fixed-scope service rather than a "
            "subscription product. The audience mixes founder-led buyers and "
            "business-unit sponsors, and no employee count is stated. The only "
            "commercial path is Book a demo or contact sales, with no public "
            "price list. Proof is limited to unnamed testimonials and a CRM "
            "integration mention, without named case studies or an integration "
            "marketplace. A=22, B=12, C=10, D=10 → total=54"
        ),
        missing_information=(
            "Employee count not mentioned",
            "No public pricing tiers",
        ),
        icebreaker=(
            "Harborline Partners starting each build with a discovery workshop, "
            "then delivering a TypeScript and Postgres application, is a concrete "
            "delivery detail."
        ),
        suggested_angle=(
            "Frame the conversation around shipping the internal tools Harborline "
            "already builds for B2B clients, and acknowledge that pricing is only "
            "available through a partner conversation."
        ),
        cited_detail="discovery workshop",
        evidence_anchors=(
            "hybrid",
            "Book a demo",
            "TypeScript",
            "no public price list",
        ),
    ),
    ScoringEvalCase(
        fixture_name="disqualified_b2c.md",
        company_name="Willow & Grain",
        industry="B2C eCommerce – Furniture and Home Goods",
        target_audience="Individual consumers furnishing apartments and family homes",
        value_proposition=(
            "Willow & Grain sells furniture and home goods directly to individual "
            "consumers, with delivery to the door and no software product."
        ),
        pain_points=(
            "Waiting weeks for a sofa to arrive at home.",
            "Showroom pieces that look different in the living room.",
            "Replacing a single damaged cushion without reordering the whole sofa.",
        ),
        fit_tier="Disqualified",
        fit_score=11,
        dimension_scores=(0, 4, 4, 3),
        business_model_band=(0, 0),
        scoring_rationale=(
            "Willow & Grain sells furniture and home goods to individual consumers "
            "and states that it has no business plan, team workspace, or software "
            "product, so the business model is incompatible and scores zero. The "
            "audience is household shoppers. Shelf prices such as the Sunday Sofa "
            "at $1,240 are consumer catalog prices, not a B2B plan structure. "
            "Reviews are two unnamed shopper quotes with no technical or "
            "customer-logo proof. A=0, B=4, C=4, D=3 → total=11"
        ),
        missing_information=(
            "Employee count not mentioned",
            "No B2B offering is described",
        ),
        icebreaker=(
            "The Sunday Sofa at $1,240 in the Willow & Grain catalog is a consumer "
            "furniture purchase, not a B2B subscription."
        ),
        suggested_angle=(
            "Do not pursue. The page is a consumer furniture and home-goods "
            "catalog with no business-facing product."
        ),
        cited_detail="Sunday Sofa",
        evidence_anchors=(
            "furniture",
            "home goods",
            "individual consumers",
            "no software product",
        ),
    ),
)

_REJECTED_INPUTS: tuple[RejectedScoringInput, ...] = (
    RejectedScoringInput(
        case_id="score-below-zero",
        fit_tier="Tier 1 (High)",
        fit_score=-1,
        error_loc=("fit_score",),
        error_type="greater_than_equal",
    ),
    RejectedScoringInput(
        case_id="score-above-100",
        fit_tier="Disqualified",
        fit_score=101,
        error_loc=("fit_score",),
        error_type="less_than_equal",
    ),
    RejectedScoringInput(
        case_id="unknown-tier",
        fit_tier="Tier 4 (Ultra)",
        fit_score=80,
        error_loc=("fit_tier",),
        error_type="literal_error",
    ),
    RejectedScoringInput(
        case_id="lowercase-tier",
        fit_tier="high",
        fit_score=90,
        error_loc=("fit_tier",),
        error_type="literal_error",
    ),
)

_RUBRIC_EDGES: tuple[tuple[int, FitTier], ...] = (
    (100, "Tier 1 (High)"),
    (75, "Tier 1 (High)"),
    (74, "Tier 2 (Medium)"),
    (50, "Tier 2 (Medium)"),
    (49, "Tier 3 (Low)"),
    (25, "Tier 3 (Low)"),
    (24, "Disqualified"),
    (0, "Disqualified"),
)


@pytest.fixture(autouse=True)
def _reject_outbound_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the suite opens a TCP connection.

    Golden labels are validated locally. A socket means a future edit started
    calling OpenAI.
    """

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("scoring regression attempted a network connection")

    monkeypatch.setattr(socket, "create_connection", _refuse)


def _assert_page_grounds_label(case: ScoringEvalCase, page: str) -> None:
    """Require company facts and the icebreaker citation to come from the page."""
    assert page.strip(), f"{case.fixture_name} is empty"
    assert case.company_name in page, case.fixture_name
    assert case.cited_detail in page, case.fixture_name
    assert case.cited_detail in case.icebreaker, case.fixture_name
    for anchor in case.evidence_anchors:
        assert anchor in page, f"{case.fixture_name} missing anchor {anchor!r}"
    for pain_point in case.pain_points:
        assert pain_point in page, (
            f"{case.fixture_name} missing pain point {pain_point!r}"
        )


def _parsed_subscores(rationale: str) -> tuple[int, int, int, int, int]:
    """Extract the rubric arithmetic line from ``scoring_rationale``."""
    matches = list(_SCORE_SUMMARY.finditer(rationale))
    assert len(matches) == 1, (
        "scoring_rationale must contain exactly one sub-score summary"
    )
    found = matches[0]
    subscores = tuple(int(found.group(name)) for name in ("a", "b", "c", "d"))
    total = int(found.group("total"))
    assert len(subscores) == 4
    return subscores[0], subscores[1], subscores[2], subscores[3], total


@pytest.mark.parametrize(
    "case",
    _EVAL_CASES,
    ids=[case.fixture_name for case in _EVAL_CASES],
)
def test_golden_fixture_satisfies_rubric_and_schema(case: ScoringEvalCase) -> None:
    """A golden page label must be grounded, in-band, and schema-stable."""
    page = case.load_page()
    _assert_page_grounds_label(case, page)

    payload = case.to_payload()
    assert isinstance(payload, EnrichedLeadPayload)
    assert isinstance(payload.analysis, CompanyAnalysis)
    assert isinstance(payload.scoring, LeadScoring)
    assert isinstance(payload.outreach, OutreachStrategy)

    restored = EnrichedLeadPayload.model_validate_json(payload.model_dump_json())
    assert restored == payload

    scoring = payload.scoring
    lower, upper = _TIER_BOUNDS[case.fit_tier]
    assert lower <= scoring.fit_score <= upper
    assert tier_for_score(scoring.fit_score) == scoring.fit_tier == case.fit_tier

    axis_a, axis_b, axis_c, axis_d, total = _parsed_subscores(scoring.scoring_rationale)
    subscores = (axis_a, axis_b, axis_c, axis_d)
    assert subscores == case.dimension_scores
    assert total == sum(subscores) == scoring.fit_score == case.fit_score
    for awarded, cap in zip(subscores, _DIMENSION_CAPS, strict=True):
        assert 0 <= awarded <= cap

    band_low, band_high = case.business_model_band
    assert band_low <= axis_a <= band_high
    if scoring.fit_tier == "Disqualified":
        assert axis_a == 0

    assert payload.company_name == case.company_name
    assert payload.analysis.pain_points == list(case.pain_points)
    assert scoring.missing_information == list(case.missing_information)


@pytest.mark.parametrize(
    ("fit_score", "fit_tier"),
    _RUBRIC_EDGES,
    ids=[
        "tier1-upper",
        "tier1-lower",
        "tier2-upper",
        "tier2-lower",
        "tier3-upper",
        "tier3-lower",
        "disqualified-upper",
        "disqualified-lower",
    ],
)
def test_rubric_band_edges_are_schema_valid(fit_score: int, fit_tier: FitTier) -> None:
    """Inclusive band edges must map to one tier and pass ``LeadScoring``."""
    assert tier_for_score(fit_score) == fit_tier
    scoring = LeadScoring.model_validate(
        {
            "fit_tier": fit_tier,
            "fit_score": fit_score,
            "scoring_rationale": f"Boundary probe at {fit_score}.",
            "missing_information": [],
        }
    )
    assert scoring.fit_score == fit_score
    assert scoring.fit_tier == fit_tier


@pytest.mark.parametrize(
    "rejected",
    _REJECTED_INPUTS,
    ids=[item.case_id for item in _REJECTED_INPUTS],
)
def test_lead_scoring_rejects_out_of_contract_values(
    rejected: RejectedScoringInput,
) -> None:
    """Scores outside 0–100 and unknown tiers must raise ``ValidationError``."""
    with pytest.raises(ValidationError) as exc_info:
        LeadScoring.model_validate(
            {
                "fit_tier": rejected.fit_tier,
                "fit_score": rejected.fit_score,
                "scoring_rationale": "Boundary probe that must be rejected.",
                "missing_information": [],
            }
        )

    matched = [
        err
        for err in exc_info.value.errors()
        if err["loc"] == rejected.error_loc and err["type"] == rejected.error_type
    ]
    assert matched, exc_info.value.errors()


def test_lead_scoring_schema_matches_dto_contract() -> None:
    """The JSON schema published to structured outputs must keep the DTO shape."""
    schema = EnrichedLeadPayload.model_json_schema()
    assert set(schema["required"]) == {
        "company_name",
        "analysis",
        "scoring",
        "outreach",
    }

    definitions = schema["$defs"]
    assert set(definitions) == {"CompanyAnalysis", "LeadScoring", "OutreachStrategy"}

    scoring = definitions["LeadScoring"]
    assert scoring["properties"]["fit_tier"]["enum"] == list(_TIER_BOUNDS)
    assert scoring["properties"]["fit_score"]["type"] == "integer"
    assert scoring["properties"]["fit_score"]["minimum"] == 0
    assert scoring["properties"]["fit_score"]["maximum"] == 100
    assert set(scoring["required"]) == {"fit_tier", "fit_score", "scoring_rationale"}

    analysis = definitions["CompanyAnalysis"]
    assert set(analysis["required"]) == {
        "industry",
        "target_audience",
        "value_proposition",
        "pain_points",
    }
    outreach = definitions["OutreachStrategy"]
    assert set(outreach["required"]) == {"icebreaker", "suggested_angle"}


def test_offline_bands_match_published_prompt() -> None:
    """The in-suite oracle must describe the same bands the model is given."""
    found: dict[str, tuple[int, int]] = {
        tier: (int(lower), int(upper))
        for tier, lower, upper in _PROMPT_BAND.findall(SYSTEM_PROMPT_TEMPLATE)
    }
    assert found == _TIER_BOUNDS


def test_rubric_bands_partition_scores_from_zero_to_one_hundred() -> None:
    """Every integer score from 0 through 100 belongs to exactly one tier."""
    covered: set[int] = set()
    for lower, upper in _TIER_BOUNDS.values():
        band = set(range(lower, upper + 1))
        assert covered.isdisjoint(band)
        covered |= band
    assert covered == set(range(101))


def test_eval_directory_matches_registered_cases() -> None:
    """Every Markdown fixture on disk must be a registered golden case."""
    on_disk = {path.name for path in _FIXTURE_DIR.glob("*.md")}
    registered = {case.fixture_name for case in _EVAL_CASES}
    assert on_disk == registered
