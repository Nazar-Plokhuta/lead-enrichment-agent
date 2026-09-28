# Architecture Specification: B2B Lead Enrichment & Scoring Agent

## 1. High-Level Flow
1. **Input**: Target company URL.
2. **Scraper (`src/scraper/`)**: Headless browser (Playwright Async) navigates to URL with ad/media blocking -> retrieves rendered DOM.
3. **Normalizer (`src/scraper/cleaner.py`)**: Trafilatura parses DOM -> strips navigation, cookies, footers -> extracts clean Markdown (budget: <= 4,000 tokens).
4. **LLM Engine (`src/llm/`)**: OpenAI (`gpt-4o-mini`) via Structured Outputs (`client.beta.chat.completions.parse`) validates payload into `EnrichedLeadPayload`.
5. **Persistence (`src/storage/`)**: Asynchronous SQLite (`aiosqlite`) persists structured lead record and execution state.

---

## 2. Directory Layout & Module Responsibilities
- `src/config.py`: Environment settings validated via `pydantic-settings` (`OPENAI_API_KEY`, concurrency limits, DB path).
- `src/core/`: Custom exception classes and structured logging setup.
- `src/scraper/`:
  - `browser.py`: Playwright lifecycle management with network asset filtering.
  - `fetcher.py`: Page navigation, explicit wait strategies, and anti-hang timeouts.
  - `cleaner.py`: Raw HTML to normalized Markdown transformation.
- `src/llm/`:
  - `schemas.py`: Pydantic models for extraction contracts.
  - `prompts.py`: Versioned strict ICP evaluation prompts.
  - `client.py`: OpenAI structured client wrapper with retry handling.
- `src/storage/`:
  - `database.py`: `aiosqlite` connection manager and schema migrations.
  - `repository.py`: CRUD operations for lead entities.
- `src/main.py`: CLI orchestration entry point handling concurrency via `asyncio.Semaphore`.
- `tests/`:
  - `fixtures/eval/`: Golden Markdown pages (`tier1_saas.md`, `tier2_consulting.md`, `disqualified_b2c.md`) that stand in for scraped content.
  - `test_schemas.py`: Construction and range-rejection checks for the extraction DTOs.
  - `test_scoring_regression.py`: Offline rubric regression — tier bands, evidence grounding, negative schema cases, and JSON-schema stability.
- `pytest.ini`: Sets `pythonpath = .` and `testpaths = tests` so the suite imports `src` without an editable install.

--- 

## 3. Data Contracts (Pydantic DTOs)

```python
from typing import Literal
from pydantic import BaseModel, Field

class CompanyAnalysis(BaseModel):
    industry: str = Field(description="Primary vertical or industry")
    target_audience: str = Field(description="Target persona (SMB, Mid-Market, Enterprise)")
    value_proposition: str = Field(description="Core value offering in 1-2 clear sentences")
    pain_points: list[str] = Field(description="Top 3 customer problems found on the page")

class LeadScoring(BaseModel):
    fit_tier: Literal["Tier 1 (High)", "Tier 2 (Medium)", "Tier 3 (Low)", "Disqualified"]
    fit_score: int = Field(ge=0, le=100, description="Deterministic ICP match score (0-100)")
    scoring_rationale: str = Field(description="Fact-based chain-of-thought ending in one A/B/C/D arithmetic line")
    missing_information: list[str] = Field(default_factory=list, description="Information missing to score accurately")

class OutreachStrategy(BaseModel):
    icebreaker: str = Field(description="1-2 highly personalized lines referencing verified facts on the site")
    suggested_angle: str = Field(description="Recommended positioning angle for sales outreach")

class EnrichedLeadPayload(BaseModel):
    company_name: str
    analysis: CompanyAnalysis
    scoring: LeadScoring
    outreach: OutreachStrategy
```

`missing_information` defaults to an empty list and is omitted from `LeadScoring`'s JSON-schema `required` array. `fit_tier` serialises as an enum of the four literals above. `fit_score` serialises as an integer with `minimum` 0 and `maximum` 100. Nested `$defs` are exactly `CompanyAnalysis`, `LeadScoring`, and `OutreachStrategy`. The offline suite in §6 treats `EnrichedLeadPayload.model_json_schema()` as this contract.

---

## 4. SQLite Schema (leads table)

```
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    domain TEXT NOT NULL,
    company_name TEXT,
    fit_score INTEGER,
    fit_tier TEXT,
    enriched_data JSON,
    raw_markdown TEXT,
    status TEXT CHECK(status IN ('PENDING', 'PROCESSED', 'FAILED')) DEFAULT 'PENDING',
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_leads_domain ON leads(domain);
CREATE INDEX IF NOT EXISTS idx_leads_fit_score ON leads(fit_score);
```

---

## 5. Architectural Invariants (Non-Negotiable)

1. Decoupled Modules: The scraper never imports database or LLM modules; the LLM module never performs network calls to scraped sites.
2. Strict Async: No synchronous file or network I/O (requests, time.sleep are strictly forbidden).
3. No Unvalidated Dicts: Data moving between LLM and storage must pass through EnrichedLeadPayload.

---

## 6. Evaluation & Regression Testing Architecture

The regression suite locks the same Pydantic contract and the same published tier bands the live OpenAI path uses, on fixtures that live in the repository, so a schema or rubric edit fails CI before any token is spent.

### 6.1 Golden Dataset Strategy

`tests/fixtures/eval/` holds hand-authored Markdown pages. They occupy the same role as Trafilatura output: substantive page text, already free of navigation chrome, passed to the rubric as the sole evidence source. Each file is registered as a `ScoringEvalCase` in `tests/test_scoring_regression.py`. `to_payload()` builds a plain dictionary and runs it through `EnrichedLeadPayload.model_validate`. That is the same Pydantic contract the OpenAI parse path enforces, exercised on a local dict so the suite stays offline. A directory test requires the `*.md` names on disk and the registered fixture names to be the same set. An unregistered page, or a registered name whose file is gone, fails the suite.

| Fixture | Company | Archetype | Score | Tier | Dimension A band |
|---|---|---|---|---|---|
| `tier1_saas.md` | Forgeboard | Per-seat B2B SaaS, public plans, named customers, SOC 2 | 92 | Tier 1 (High) | 30–35 (awarded 33) |
| `tier2_consulting.md` | Harborline Partners | Tech-enabled consulting, demo-only commercial path | 54 | Tier 2 (Medium) | 15–25 (awarded 22) |
| `disqualified_b2c.md` | Willow & Grain | Consumer furniture catalog, no software product | 11 | Disqualified | 0 (incompatible model) |

The test asserts grounding on the case itself. It requires `company_name`, every `pain_points` entry, every evidence anchor, and the icebreaker's cited detail (`Helio Payments`, `discovery workshop`, `Sunday Sofa`) to occur in the page text, and the cited detail to occur in `icebreaker`. A JSON round-trip (`model_dump_json` then `model_validate_json`) must reproduce the payload, which checks that the DTO stays serialisable in the shape `enriched_data` stores.

Sub-scores are parsed from a single arithmetic line inside `scoring_rationale`. That line must match `A=<int>, B=<int>, C=<int>, D=<int> → total=<int>`, and it must appear exactly once. Those four integers must equal the case's `dimension_scores`, respect the ceilings 35 / 25 / 20 / 20, and sum to both the stated total and `fit_score`. Dimension A must also sit inside the archetype band in the table. A Disqualified case must award 0 on A, matching the prompt rule that an incompatible business model zeroes that dimension and forces the tier.

### 6.2 Rubric Boundary Enforcement

Bands are inclusive and taken from the v2.0 `FIT TIER MAPPING` block in `SYSTEM_PROMPT_TEMPLATE`. `tier_for_score` is the in-suite oracle. `test_offline_bands_match_published_prompt` parses the same four lines out of the prompt (en dash, `U+2013`) and requires the two maps to be equal. Editing a boundary in the prompt without updating the oracle fails CI.

| Tier | Inclusive band |
|---|---|
| Tier 1 (High) | 75–100 |
| Tier 2 (Medium) | 50–74 |
| Tier 3 (Low) | 25–49 |
| Disqualified | 0–24 |

The edges themselves are schema-valid `LeadScoring` values, parametrised as eight cases: 100 and 75 (Tier 1), 74 and 50 (Tier 2), 49 and 25 (Tier 3), 24 and 0 (Disqualified). A separate test walks every integer from 0 through 100 and requires the four bands to be disjoint and exhaustive. Tier 3 has edge coverage and partition coverage. The golden pages in §6.1 cover Tier 1, Tier 2, and Disqualified, which are the three archetype outcomes the current fixture set labels.

Negative schema assertions construct a `LeadScoring` body that must raise `pydantic.ValidationError`, and they match `loc` plus `type` on the error list:

| Case | Input | Expected error |
|---|---|---|
| `score-below-zero` | `fit_score=-1` | `loc=("fit_score",)`, `type="greater_than_equal"` |
| `score-above-100` | `fit_score=101` | `loc=("fit_score",)`, `type="less_than_equal"` |
| `unknown-tier` | `fit_tier="Tier 4 (Ultra)"` | `loc=("fit_tier",)`, `type="literal_error"` |
| `lowercase-tier` | `fit_tier="high"` | `loc=("fit_tier",)`, `type="literal_error"` |

`test_schemas.py` repeats the upper-bound rejection (`fit_score=150`) via the model constructor, and builds one fully populated `EnrichedLeadPayload` (company, `CompanyAnalysis`, `LeadScoring`, `OutreachStrategy`) as a smoke check that the object graph still assembles.

`test_lead_scoring_schema_matches_dto_contract` reads `EnrichedLeadPayload.model_json_schema()` and locks the document Structured Outputs receives:

- Top-level `required`: `company_name`, `analysis`, `scoring`, `outreach`.
- `$defs` keys: `CompanyAnalysis`, `LeadScoring`, `OutreachStrategy`.
- `LeadScoring.fit_tier.enum` equals the four tier literals, in oracle order.
- `LeadScoring.fit_score` is an integer with `minimum` 0 and `maximum` 100.
- `LeadScoring.required` is `fit_tier`, `fit_score`, `scoring_rationale`. `missing_information` stays optional because the field has a default.
- `CompanyAnalysis.required`: `industry`, `target_audience`, `value_proposition`, `pain_points`.
- `OutreachStrategy.required`: `icebreaker`, `suggested_angle`.

### 6.3 Offline Determinism Guarantees

Every test in `test_scoring_regression.py` runs under an autouse fixture that replaces `socket.create_connection` with a function that raises `AssertionError`. A future edit that opens a TCP connection during collection or execution fails the suite. Golden labels are dictionaries validated by Pydantic. The module imports `SYSTEM_PROMPT_TEMPLATE` and the four DTOs (`EnrichedLeadPayload`, `CompanyAnalysis`, `LeadScoring`, `OutreachStrategy`). `LLMClient` construction, `OPENAI_API_KEY` reads, and `client.beta.chat.completions.parse` calls sit outside this module.

That split is deliberate. Live enrichment remains variable in free-text fields because `LLMClient` uses temperature `0.1`, and it depends on provider availability. The regression job's result is a pure function of the fixture files, the prompt text, and the DTO definitions. CI can therefore install `pytest` with a mock API key, run `pytest`, and treat a failure as a contract diff: a moved band, a renamed field, a `Literal` change, a fixture whose evidence is missing from the page, or sub-scores that disagree with `fit_score`. Provider latency and token spend stay on the manual enrichment path.