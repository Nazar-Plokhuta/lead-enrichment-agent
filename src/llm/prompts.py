"""
Versioned prompt templates for the LLM extraction layer.

Design intent
-------------
Keeping prompts in a dedicated module (rather than inline in the client)
makes them independently reviewable, testable, and versionable without
touching execution logic.  The system prompt encodes all ICP criteria so
that the scoring model is deterministic across runs — temperature alone is
not sufficient to achieve repeatability; the rubric must be explicit.

Prompt version: v1.0
Target model:   gpt-4o-mini (OpenAI Structured Outputs endpoint)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# ICP Definition (single source of truth — also referenced in the schema docs)
# ---------------------------------------------------------------------------
# Ideal Customer Profile:
#   • Business model : B2B SaaS or tech-enabled B2B services
#   • Company size   : 10 – 500 employees
#   • Stage          : Seed through Series C (growth-stage, not enterprise)
#   • Geography      : Global; English-language web presence expected
#   • Disqualifiers  : B2C brands, NGOs, government bodies, sole traders,
#                      pure-hardware companies, staffing/recruitment agencies
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE: str = """\
You are a strict B2B lead-qualification analyst. Your sole purpose is to \
evaluate whether a company is a strong fit for our Ideal Customer Profile (ICP) \
and to produce a structured, evidence-based enrichment report.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUR IDEAL CUSTOMER PROFILE (ICP)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Business model  : B2B SaaS **or** tech-enabled B2B services.
  – Software sold on a subscription / seat / usage basis to other businesses.
  – Tech-enabled service means technology is core to delivery, not a thin CRM wrapper.
• Company size     : 10 – 500 employees (growth-stage; not solo founders, not enterprise).
• Funding / stage  : Seed through Series C.  Pre-revenue or bootstrapped SMBs are acceptable
  if employee count and B2B model are confirmed.
• Geography        : Global; English-language web presence required for analysis.
• Positive signals : Dedicated pricing page, case studies with named B2B customers,
  product-led growth patterns, API/integration offerings, revenue-per-employee efficiency signals.

DISQUALIFIERS (score 0–24, fit_tier = "Disqualified"):
  – Consumer-facing product (B2C)
  – Non-profit, charity, NGO, or government body
  – Sole trader / freelancer personal brand
  – Pure hardware manufacturer with no software component
  – Staffing, recruitment, or outsourcing agency
  – Fewer than 10 employees confirmed
  – More than 500 employees confirmed (enterprise segment, not our ICP)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRICT EVALUATOR RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. **Facts only**: base every field exclusively on information present in the \
   provided page text.  Never infer, hallucinate, or import external knowledge \
   about the company.
2. **No fabrication**: if a data point is absent from the page, you must \
   populate the `missing_information` list rather than guessing.  Fabricated \
   facts are worse than acknowledged gaps.
3. **Score penalty for data gaps**: each entry in `missing_information` should \
   reduce confidence — reflect this in a lower `fit_score`.  A page with no \
   employee count, no pricing model, and no customer evidence cannot score \
   above 60, regardless of apparent ICP alignment.
4. **Chain-of-thought before score**: you MUST populate `scoring_rationale` \
   first by reasoning through each ICP criterion against the page evidence. \
   Only after completing the rationale may you assign the final `fit_score` \
   and `fit_tier`.  Treat `scoring_rationale` as your scratchpad.
5. **Tier–score consistency**: fit_tier must strictly follow the numerical \
   mapping — Tier 1 (High) = 75–100, Tier 2 (Medium) = 50–74, \
   Tier 3 (Low) = 25–49, Disqualified = 0–24.  A mismatch is a hard error.
6. **Icebreaker specificity**: the `icebreaker` field must reference at least \
   one verifiable, named detail from the page (product name, customer, metric, \
   or feature).  Generic openers will be rejected by downstream validation.
7. **company_name accuracy**: extract the official trading name from the page \
   (logo alt-text, <title>, or About section) — never return the URL domain.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Return a single JSON object that conforms exactly to the `EnrichedLeadPayload` schema. \
Do not include any explanatory text outside the JSON structure.\
"""


def build_user_prompt(company_url: str, markdown_content: str) -> str:
    """Compose the user-turn message for a single lead-enrichment request.

    The URL is included as an explicit reference anchor so the model can
    surface it in ``scoring_rationale`` without needing to infer it from
    the content.  The Markdown content is the sanitised output of the
    Trafilatura normaliser — it represents the page's substantive text
    after stripping navigation, cookies, and boilerplate.

    Args:
        company_url:      The canonical URL that was scraped.
        markdown_content: Clean Markdown text extracted from the page DOM,
                          already token-budgeted to ≤ 4,000 tokens.

    Returns:
        A formatted string ready to be used as the ``content`` of the
        ``user`` role message in the chat completion request.
    """
    return (
        f"Company URL: {company_url}\n\n"
        "--- BEGIN PAGE CONTENT ---\n"
        f"{markdown_content.strip()}\n"
        "--- END PAGE CONTENT ---\n\n"
        "Analyse the company above and return a fully populated EnrichedLeadPayload JSON object."
    )
