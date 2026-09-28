# B2B Lead Enrichment & Scoring Agent

[![Release](https://img.shields.io/github/v/release/Nazar-Plokhuta/lead-enrichment-agent?style=for-the-badge&color=2563EB&logo=github&logoColor=white)](https://github.com/Nazar-Plokhuta/lead-enrichment-agent/releases)
[![CI Pipeline](https://img.shields.io/github/actions/workflow/status/Nazar-Plokhuta/lead-enrichment-agent/ci.yml?branch=main&style=for-the-badge&logo=githubactions&logoColor=white&label=CI%20Pipeline)](https://github.com/Nazar-Plokhuta/lead-enrichment-agent/actions)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Pydantic v2](https://img.shields.io/badge/Pydantic-v2-E92063?style=for-the-badge&logo=pydantic&logoColor=white)](https://docs.pydantic.dev/)

A high-performance, fully asynchronous pipeline that crawls target company domains, extracts semantic Markdown, and computes deterministic ICP fit scores using LLM Structured Outputs.  Results are persisted to SQLite with full audit trails, idempotency guarantees, and bounded concurrency.

![Lead Enrichment Agent CLI Demo](assets/demo.gif)

---

## Architecture

```mermaid
flowchart LR
    A([URLs via CLI]) --> B

    subgraph Scraper ["src/scraper/"]
        B["BrowserManager\nPlaywright Chromium\nRoute interception"]
        B --> C["fetch_page_markdown\ndomcontentloaded wait\nAnti-hang timeout"]
        C --> D["extract_markdown\nTrafilatura normalisation\n≤ 15 000 char budget"]
    end

    D --> E

    subgraph LLM ["src/llm/"]
        E["LLMClient\nclient.beta.chat.completions.parse"]
        E --> F["EnrichedLeadPayload\nPydantic v2 DTOs\nStructured Outputs contract"]
    end

    F --> G

    subgraph Storage ["src/storage/"]
        G["LeadRepository\ninsert_pending_lead\nsave_lead_success / mark_lead_failed"]
        G --> H[("leads.db\naiosqlite\nasync SQLite")]
    end

    subgraph Orchestrator ["src/main.py"]
        I["asyncio.Semaphore\nMAX_CONCURRENT_SCRAPES\nIdempotency guard"]
    end

    A --> I
    I -->|"acquire slot"| B
    I -->|"--force flag"| G
```

**Data flow summary**:

```
CLI URLs
  └──► asyncio.Semaphore (concurrency gate)
        └──► Playwright Chromium  (headless, asset-blocked)
              └──► Trafilatura     (HTML → clean Markdown)
                    └──► OpenAI Structured Outputs  (Markdown → EnrichedLeadPayload)
                          └──► aiosqlite            (validated payload → leads.db)
```

---

## Key Technical Features

### Fully Asynchronous I/O

Every network and database operation runs on the asyncio event loop with no blocking calls.  Playwright's `async_api`, the `openai.AsyncOpenAI` client, and `aiosqlite` are used throughout.  `asyncio.gather` fans out all URL tasks in parallel, bounded by a shared `asyncio.Semaphore` to prevent browser-process exhaustion and OpenAI rate-limit violations.

### Strict Schema Validation — Zero Raw JSON Parsing

The extraction contract is defined as a Pydantic v2 model hierarchy (`EnrichedLeadPayload`) and passed directly to `client.beta.chat.completions.parse` as the `response_format`.  The OpenAI SDK injects the JSON schema into the API call and validates the model response before returning a typed Python object.  There are no calls to `json.loads()`, no dict-to-model coercion steps, and no secondary validation passes anywhere in the codebase.

### Resilient Resource Management

| Mechanism | Implementation |
|---|---|
| Asset blocking | Context-level Playwright route interception aborts `image`, `media`, `font`, and `stylesheet` requests before they are fetched. |
| Anti-hang timeout | `domcontentloaded` wait strategy with a hard `PAGE_TIMEOUT_MS` ceiling; never waits for analytics beacons or SSE streams to close. |
| Zombie prevention | `BrowserManager.__aexit__` closes context → browser → Playwright driver in dependency order with independent `try/finally` blocks. |
| Error isolation | Domain exceptions (`ScrapeError`, `LLMExtractionError`) insulate the orchestrator from Playwright and OpenAI SDK internals. |

### Concurrency & Idempotency Control

`asyncio.Semaphore(MAX_CONCURRENT_SCRAPES)` bounds the number of live browser contexts and simultaneous OpenAI requests.  The idempotency check (`get_lead_by_url`) runs *before* semaphore acquisition so that already-processed URLs consume zero concurrency slots.  `INSERT OR IGNORE` on the `url` UNIQUE constraint eliminates the TOCTOU race when concurrent tasks attempt to insert the same URL simultaneously.

---

## Project Structure

```
lead-enrichment-agent/
├── src/
│   ├── config.py              # BaseSettings singleton (pydantic-settings)
│   ├── main.py                # Async CLI orchestrator
│   ├── core/
│   │   └── exceptions.py      # Domain exception hierarchy
│   ├── scraper/
│   │   ├── browser.py         # Playwright Chromium lifecycle & route interception
│   │   ├── fetcher.py         # Page navigation, wait strategy, error mapping
│   │   └── cleaner.py         # HTML → Markdown via trafilatura
│   ├── llm/
│   │   ├── schemas.py         # Pydantic v2 DTOs (Structured Outputs contract)
│   │   ├── prompts.py         # Versioned ICP rubric & prompt templates
│   │   └── client.py          # AsyncOpenAI wrapper
│   └── storage/
│       ├── database.py        # Schema DDL, init_db, connection context manager
│       └── repository.py      # CRUD against the leads table
├── tests/
│   ├── fixtures/
│   │   └── eval/              # Golden Markdown pages for offline rubric regression
│   │       ├── tier1_saas.md
│   │       ├── tier2_consulting.md
│   │       └── disqualified_b2c.md
│   ├── test_schemas.py        # Pydantic v2 DTO smoke tests
│   └── test_scoring_regression.py  # Deterministic ICP scoring regression suite
├── docs/
│   └── internal/
│       ├── architecture.md    # High-level design specification
│       └── state.md           # Current implementation state & decision log
├── .env.example               # Environment variable reference
├── pytest.ini                 # Pytest discovery (`pythonpath`, `testpaths`)
└── requirements.txt           # Pinned runtime dependencies
```

---

## Setup & Quickstart

### Prerequisites

- **Python 3.11+**
- **Playwright Chromium** (installed separately from the Python package)

### 1. Clone and install dependencies

```bash
git clone <repo-url>
cd lead-enrichment-agent
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Configure the environment

Copy `.env.example` to `.env` and fill in real values:

```bash
cp .env.example .env
```

`.env.example` reference:

```ini
OPENAI_API_KEY=sk-replace-me

# Optional overrides (defaults shown)
OPENAI_MODEL=gpt-4o-mini
# OPENAI_BASE_URL=https://openrouter.ai/api/v1   # uncomment to use OpenRouter
DATABASE_PATH=leads.db
MAX_CONCURRENT_SCRAPES=3
PAGE_TIMEOUT_MS=20000
LOG_LEVEL=INFO
```

Setting `OPENAI_BASE_URL` routes all LLM calls through any OpenAI-compatible
endpoint (OpenRouter, Azure proxy, LM Studio) without code changes.

### 3. Run the pipeline

```bash
# Enrich one or more company URLs
python -m src.main https://linear.app https://vercel.com

# Re-enrich URLs that were already processed
python -m src.main https://linear.app --force
```

**CLI options**:

| Argument | Description |
|---|---|
| `URL [URL ...]` | One or more fully-qualified company URLs to enrich. |
| `--force` | Re-enrich URLs already in `PROCESSED` state; without this flag they are skipped. |

**Console output on completion**:

As of **v1.1.0**, the agent renders a fully styled terminal experience powered by [`rich`](https://github.com/Textualize/rich):

- **Coloured telemetry logs** — each pipeline stage (scrape → extract → persist) is printed with severity-coloured prefixes and structured context (URL, elapsed time, status).
- **Live execution status** — a `rich` live display tracks in-flight tasks in real time, showing which URLs are currently being scraped or scored.
- **Native summary table** — on completion, a formatted table is printed with per-URL outcomes (company name, fit score, fit tier, status), followed by aggregate counters for enriched, failed, and skipped records.

### 4. Query results

```bash
sqlite3 leads.db "SELECT url, company_name, fit_score, fit_tier, status FROM leads ORDER BY fit_score DESC;"
```

### Testing & Verification

`requirements.txt` pins runtime libraries only.  Verification tools match the
CI install in `.github/workflows/ci.yml`:

```bash
pip install ruff pytest pytest-asyncio
pytest
ruff check src/
```

`pytest.ini` sets `pythonpath = .` and `testpaths = tests`, so a bare `pytest`
collects the suite and imports `src` without an editable install.  CI runs
`ruff check src/` and then `pytest tests/ -v`, exporting a mock
`OPENAI_API_KEY` that the tests never read.

**Deterministic offline regression.**  `tests/test_scoring_regression.py`
validates ICP rubric scoring and the structured-output contract on local data.
Golden Markdown pages in `tests/fixtures/eval/` stand in for Trafilatura
output.  Each page is paired with an expected `EnrichedLeadPayload` label.
The suite checks:

- Schema acceptance through `EnrichedLeadPayload.model_validate` — the same
  Pydantic contract `client.beta.chat.completions.parse` enforces — plus a
  JSON round-trip (`model_dump_json` → `model_validate_json`).
- Tier consistency against the v2.0 inclusive bands (Tier 1: 75–100, Tier 2:
  50–74, Tier 3: 25–49, Disqualified: 0–24), including the eight boundary
  scores 0, 24, 25, 49, 50, 74, 75, and 100.
- Rubric arithmetic.  `scoring_rationale` must contain exactly one
  `A=…, B=…, C=…, D=… → total=…` line.  Sub-scores stay inside the dimension
  ceilings (35 / 25 / 20 / 20), sum to `fit_score`, and fall in the
  business-model band for that archetype.  A Disqualified label awards 0 on
  dimension A.
- Evidence grounding.  Company name, pain points, evidence anchors, and the
  detail cited by `icebreaker` must appear in the fixture page.
- Negative schema assertions.  `fit_score` below 0, `fit_score` above 100, and
  tier labels outside the `Literal` (`Tier 4 (Ultra)`, `high`) raise
  `ValidationError` with the expected Pydantic error type.

An autouse fixture replaces `socket.create_connection`, so a TCP connect fails
the run.  Labels are validated in-process.  A green run spends no OpenAI
tokens and waits on no network round-trip.  Prompt drift is caught by parsing
the tier bands out of `SYSTEM_PROMPT_TEMPLATE` and requiring them to equal the
in-suite oracle.  `tests/test_schemas.py` adds a happy-path construction of
`EnrichedLeadPayload` and a direct rejection of an out-of-range
`LeadScoring.fit_score`.

**Current verification status**: 21 tests passing, and `ruff check src/` is clean.

| Group | Tests | What they lock |
|---|---|---|
| Golden fixtures (`tier1_saas`, `tier2_consulting`, `disqualified_b2c`) | 3 | Grounding, sub-scores, tier, DTO round-trip |
| Inclusive band edges | 8 | 0, 24, 25, 49, 50, 74, 75, 100 |
| Out-of-contract `LeadScoring` bodies | 4 | Score below 0, score above 100, unknown tier, lowercase tier |
| Contract invariants | 4 | JSON schema shape, prompt-band parity, 0–100 partition, fixture registration |
| `test_schemas.py` | 2 | Valid payload, score above 100 rejected |
| **Total** | **21** | |

---

## Example Output

Verified enrichment result for **[linear.app](https://linear.app)** — a B2B SaaS
product-development tool:

```json
{
  "company_name": "Linear",
  "analysis": {
    "industry": "B2B SaaS – Product Development Tools",
    "target_audience": "Modern product teams (10–500 employees)",
    "value_proposition": "Linear is a purpose-built product development system designed for modern teams, integrating AI workflows to streamline planning and building products.",
    "pain_points": [
      "Streamlining product planning and building processes",
      "Aligning teams with product initiatives and strategic roadmaps",
      "Automating issue routing and prioritization based on customer feedback"
    ]
  },
  "scoring": {
    "fit_tier": "Tier 1 (High)",
    "fit_score": 85,
    "scoring_rationale": "Linear operates as a B2B SaaS company focused on product development tools, which aligns with our ICP. The page indicates a dedicated focus on modern teams and AI workflows, suggesting a tech-enabled service model. The absence of employee count is noted, but the product's focus on teams implies a likely fit within the 10–500 employee range. The presence of features like automations and integrations indicates a strong product-led growth pattern. There are no disqualifying factors present, and the value proposition is clearly articulated.",
    "missing_information": [
      "Employee count not mentioned"
    ]
  },
  "outreach": {
    "icebreaker": "I noticed that Linear is designed for modern teams with AI workflows at its core, which is a game-changer for product development.",
    "suggested_angle": "Given Linear's focus on streamlining product planning and building processes, our solution could enhance your existing workflows by providing additional automation and integration capabilities that further optimize team collaboration."
  }
}
```

**Score breakdown**: 85/100 → Tier 1 (High).  The recorded rationale notes the
missing employee-count signal and confirms the remaining ICP evidence from the
page (B2B SaaS, product-led growth patterns, integrations/API, named
growth-stage customers).  This JSON is a live enrichment snapshot.  Band
edges, tier–score consistency, and the required sub-score line are locked by
the offline suite under Testing & Verification.

---

## ICP Scoring Rubric

| Score Range | Tier | Interpretation |
|---|---|---|
| 75 – 100 | Tier 1 (High) | Strong ICP fit; prioritise for immediate outreach. |
| 50 – 74 | Tier 2 (Medium) | Partial fit; engage with qualification questions. |
| 25 – 49 | Tier 3 (Low) | Weak fit; defer or monitor. |
| 0 – 24 | Disqualified | Does not meet ICP criteria (B2C, NGO, sole trader, enterprise, etc.). |

Scoring follows the v2.0 additive rubric.  `scoring_rationale` is written
first as a chain-of-thought scratchpad and must close with one sub-score line
(`A=…, B=…, C=…, D=… → total=…`).  `fit_score` is that total.  The four
ceilings are business model (35), target market (25), commercial clarity (20),
and technical proof (20).  `fit_tier` is derived from the total using the
bands above.  Gaps are recorded in `missing_information` and reduce only the
dimension they affect (employee count → B, pricing → C).

---

## Technology Stack

| Layer | Library | Version |
|---|---|---|
| Async runtime | `asyncio` (stdlib) | Python 3.11+ |
| Web scraping | `playwright` | ≥ 1.44.0 |
| HTML normalisation | `trafilatura` | ≥ 2.0.0 |
| LLM client | `openai` | ≥ 1.30.0 |
| Schema validation | `pydantic` | ≥ 2.7.0 |
| Settings | `pydantic-settings` | ≥ 2.3.0 |
| Persistence | `aiosqlite` | ≥ 0.20.0 |
| CLI & Formatting | `rich` | ≥ 13.7.0 |
