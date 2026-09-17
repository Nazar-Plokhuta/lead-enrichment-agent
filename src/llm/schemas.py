"""
Pydantic v2 data-transfer objects (DTOs) for the LLM extraction layer.

Design intent
-------------
These models serve a dual purpose:
1. They act as the ``response_format`` contract passed to
   ``client.beta.chat.completions.parse``, enforcing that the model returns
   a structurally valid payload — no raw JSON parsing, no dict manipulation.
2. They are the canonical representation of an enriched lead that travels
   through the entire pipeline (LLM → storage).  The storage layer serialises
   ``EnrichedLeadPayload`` directly; no other module redefines these shapes.

All ``Field`` descriptions are deliberately verbose because they are embedded
in the JSON schema that OpenAI's structured-output endpoint injects into the
system context, directly guiding the model's field population.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CompanyAnalysis(BaseModel):
    """Factual analysis of the company derived exclusively from page content."""

    industry: str = Field(
        description=(
            "Primary industry vertical or market category the company operates in "
            "(e.g., 'B2B SaaS – HR Tech', 'E-commerce Logistics', 'FinTech – Payments'). "
            "Infer from product descriptions and customer language; do not guess."
        )
    )
    target_audience: str = Field(
        description=(
            "The primary buyer persona and company size segment explicitly or implicitly "
            "addressed on the site (e.g., 'SMB e-commerce operators (1–50 employees)', "
            "'Mid-Market SaaS engineering teams (50–500 employees)', 'Enterprise CISOs'). "
            "Base this on pricing tiers, case-study logos, or explicit copy — not assumption."
        )
    )
    value_proposition: str = Field(
        description=(
            "The company's core value offering stated in 1–2 clear sentences. "
            "Paraphrase the headline claim using verified language from the page. "
            "Do not invent benefits not mentioned on the site."
        )
    )
    pain_points: list[str] = Field(
        description=(
            "Exactly the top 3 customer problems the product or service claims to solve, "
            "extracted verbatim or paraphrased from the page. "
            "If fewer than 3 are identifiable, list only those found and flag the gap "
            "in 'missing_information' on LeadScoring."
        )
    )


class LeadScoring(BaseModel):
    """Deterministic ICP-fit assessment grounded strictly in verified page facts."""

    fit_tier: Literal["Tier 1 (High)", "Tier 2 (Medium)", "Tier 3 (Low)", "Disqualified"] = Field(
        description=(
            "Categorical fit rating derived from the numerical fit_score: "
            "Tier 1 (High) = 75–100, Tier 2 (Medium) = 50–74, "
            "Tier 3 (Low) = 25–49, Disqualified = 0–24. "
            "Must be consistent with fit_score — never assign conflicting tier and score."
        )
    )
    fit_score: int = Field(
        ge=0,
        le=100,
        description=(
            "Deterministic ICP match score from 0 to 100. "
            "Populate scoring_rationale *before* assigning this value. "
            "Deduct points for: non-B2B model, <10 or >500 employees (if detectable), "
            "non-tech-enabled service, missing employee count, missing business model clarity. "
            "Score 0 if the company is a consumer brand, NGO, government, or sole trader."
        ),
    )
    scoring_rationale: str = Field(
        description=(
            "Detailed, fact-based chain-of-thought reasoning that justifies the fit_score. "
            "Must reference specific evidence from the page (e.g., pricing model, customer logos, "
            "technology stack mentions, team size signals). "
            "This field must be populated *prior to* assigning fit_score — treat it as the "
            "scratchpad that produces the final number."
        )
    )
    missing_information: list[str] = Field(
        default_factory=list,
        description=(
            "List of data points that were absent from the page but are required for a "
            "high-confidence ICP assessment (e.g., 'Employee count not mentioned', "
            "'Pricing model unclear — could be B2C or B2B', 'No customer case studies found'). "
            "Populate this field even for high-scoring leads if any gap exists. "
            "An empty list signals the page contained sufficient information."
        ),
    )


class OutreachStrategy(BaseModel):
    """Personalised first-touch outreach guidance derived from verified page content."""

    icebreaker: str = Field(
        description=(
            "1–2 highly personalised opening sentences for a cold outreach message. "
            "Must reference a specific, verifiable detail from the site "
            "(e.g., a named product feature, a recent case study, an explicit growth claim). "
            "Generic praise ('love what you're building') is strictly forbidden."
        )
    )
    suggested_angle: str = Field(
        description=(
            "Recommended positioning angle for the sales pitch — how our offering maps to "
            "the company's identified pain points or strategic context. "
            "Should be 1–3 sentences and grounded in the CompanyAnalysis findings."
        )
    )


class EnrichedLeadPayload(BaseModel):
    """
    Top-level DTO representing a fully enriched and scored B2B lead.

    This is the single contract that crosses the boundary from the LLM layer to
    the storage layer.  It must never be constructed manually outside of the
    LLM client — all instances originate from a validated structured-output call.
    """

    company_name: str = Field(
        description=(
            "Official company name as it appears on the website (not the URL domain). "
            "Use the name from the page title, logo alt-text, or 'About' section."
        )
    )
    analysis: CompanyAnalysis
    scoring: LeadScoring
    outreach: OutreachStrategy
