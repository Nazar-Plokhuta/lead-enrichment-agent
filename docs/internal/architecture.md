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

--- 

## 3. Data Contracts (Pydantic DTOs)

```python
from typing import List, Literal
from pydantic import BaseModel, Field

class CompanyAnalysis(BaseModel):
    industry: str = Field(description="Primary vertical or industry")
    target_audience: str = Field(description="Target persona (SMB, Mid-Market, Enterprise)")
    value_proposition: str = Field(description="Core value offering in 1-2 clear sentences")
    pain_points: List[str] = Field(description="Top 3 customer problems solved by the company")

class LeadScoring(BaseModel):
    fit_tier: Literal["Tier 1 (High)", "Tier 2 (Medium)", "Tier 3 (Low)", "Disqualified"]
    fit_score: int = Field(ge=0, le=100, description="Deterministic ICP match score (0-100)")
    scoring_rationale: str = Field(description="Strict fact-based reasoning justifying the score")
    missing_information: List[str] = Field(default_factory=list, description="Information missing to score accurately")

class OutreachStrategy(BaseModel):
    icebreaker: str = Field(description="1-2 highly personalized lines referencing verified facts on the site")
    suggested_angle: str = Field(description="Recommended positioning angle for sales outreach")

class EnrichedLeadPayload(BaseModel):
    company_name: str
    analysis: CompanyAnalysis
    scoring: LeadScoring
    outreach: OutreachStrategy
```

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