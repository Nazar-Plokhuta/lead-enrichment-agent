# Architectural State Document — B2B Lead Enrichment & Scoring Agent

> **Purpose**: Captures the current implementation state of every module, the
> rationale behind non-obvious design choices, and the invariants that must be
> preserved as the codebase evolves.  This document is a living record; update
> it whenever a design decision changes.
>
> **Last updated**: 2026-09-28  
> **Pipeline status**: End-to-end verified against live targets (linear.app).  Schema and ICP-rubric contracts are locked by 21 offline deterministic tests (`pytest`); `ruff check src/` is clean.

---

## 1. `src/config.py` — Application Settings

### Implementation

`Settings` subclasses `pydantic_settings.BaseSettings` with
`SettingsConfigDict(env_file=".env", extra="ignore")`.  All secrets are typed
as `pydantic.SecretStr`, which prevents their values from being rendered in
tracebacks, log output, or `repr()` calls.

A module-level `@lru_cache(maxsize=1)` wrapper around `get_settings()` ensures
the `.env` file is parsed exactly once per process lifetime.  Test suites can
call `get_settings.cache_clear()` to force re-evaluation against a patched
environment without restarting the process.

### Key Fields

| Field | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | *(required)* | Wrapped in `SecretStr`; passed directly to `AsyncOpenAI`. |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model identifier forwarded to the completions endpoint. |
| `OPENAI_BASE_URL` | `None` | Optional base URL override for OpenRouter or self-hosted proxies. |
| `DATABASE_PATH` | `leads.db` | Filesystem path to the SQLite database file. |
| `MAX_CONCURRENT_SCRAPES` | `3` | Upper bound for the `asyncio.Semaphore` in the orchestrator. |
| `PAGE_TIMEOUT_MS` | `20000` | Playwright navigation timeout; also the scraper's anti-hang guard. |
| `LOG_LEVEL` | `INFO` | Standard library logging level applied at process startup. |

### OpenRouter / Custom Endpoint Support

`OPENAI_BASE_URL` is forwarded to `AsyncOpenAI` only when it is not `None`:

```python
**({"base_url": self._settings.OPENAI_BASE_URL} if self._settings.OPENAI_BASE_URL else {})
```

Passing `base_url=None` explicitly to the SDK would override its internal
default with `None`, causing a runtime error.  The conditional unpacking
avoids this without special-casing the `AsyncOpenAI` constructor.  Any
OpenAI-compatible endpoint (OpenRouter, Azure OpenAI proxy, LM Studio) is
supported without code changes by setting `OPENAI_BASE_URL` in `.env`.

---

## 2. `src/scraper/` — Playwright Scraping Pipeline

### Module Map

| File | Responsibility |
|---|---|
| `browser.py` | Chromium lifecycle, route interception, context management. |
| `fetcher.py` | Page navigation, wait strategy, error mapping, DOM extraction. |
| `cleaner.py` | HTML → Markdown normalisation via trafilatura, token-budget enforcement. |

### 2.1 `browser.py` — Chromium Lifecycle

`BrowserManager` is an `async with`-compatible context manager that owns
exactly one Playwright instance, one Chromium browser, and one browser context.
It follows a strict dependency-ordered teardown sequence in `__aexit__`:
`context.browser.close()` → `playwright.stop()`, with independent try/finally
blocks to guarantee the Playwright driver process is always terminated even if
the browser close raises.

**Route interception** is registered at the context level (not per-page):

```python
await self._context.route("**/*", _abort_blocked_resources)
```

`_abort_blocked_resources` calls `route.abort()` for `image`, `media`, `font`,
and `stylesheet` resource types; all other requests pass through via
`route.continue_()`.  Context-level registration means every page opened inside
the context inherits the filter automatically — no per-page setup is required
and no resource can slip through if a page is opened by JavaScript.

`route.abort()` is used instead of returning an empty response because aborting
keeps the browser's internal request queue clean.  Empty responses can trigger
site-level JavaScript error handlers that spin up retry loops, potentially
blocking `domcontentloaded` indefinitely.

The User-Agent is set to a realistic desktop Chrome string
(`Chrome/124.0.0.0 Safari/537.36`) and the viewport to `1440×900`.  Both are
set on the browser context, not per-page, to produce a consistent fingerprint
across all navigation within the context.

### 2.2 `fetcher.py` — Page Navigation & Error Mapping

`fetch_page_markdown` is the single public function for the scraping pipeline.
It opens a fresh page against the `BrowserManager` context, navigates to the
target URL, retrieves the rendered HTML, and delegates normalisation to
`extract_markdown`.

**Wait strategy — `domcontentloaded` over `networkidle`**:

`networkidle` waits until there are no more than 2 in-flight network requests
for at least 500 ms.  On modern SaaS marketing pages this never fires because:
- Analytics endpoints (Segment, Mixpanel, Intercom) emit continuous heartbeats.
- Server-sent event streams and WebSocket connections never close.
- Asset CDNs may have slow keep-alive connections that hold the counter above 2.

`domcontentloaded` fires as soon as the HTML document is parsed and
synchronous scripts have executed — which is the moment the semantic content is
available.  Combined with the route-interception asset filter (which eliminates
most inflight requests before the budget window is measured), `domcontentloaded`
fires reliably without timing out.

**Error mapping**:  Both `PlaywrightTimeoutError` and the generic
`PlaywrightError` are caught at the navigation stage and re-raised as
`ScrapeError`.  A second try/except guards `page.content()` against renderer
crashes that occur after a successful navigation.  `page.close()` is in a
`finally` block so page handles are freed immediately regardless of outcome.

### 2.3 `cleaner.py` — HTML → Markdown Normalisation

`extract_markdown` delegates DOM boilerplate removal to trafilatura, which uses
a readability-derived algorithm to identify the primary content block and
discard navigation, footers, cookie banners, inline scripts, and ad containers.

Configuration overrides:

| Option | Value | Rationale |
|---|---|---|
| `include_tables=True` | enabled | Pricing tables and feature comparison grids carry high semantic value for ICP scoring. |
| `include_links=False` | disabled | Raw hypertext URLs add noise without semantic value for LLM extraction. |
| `include_images=False` | disabled | Alt text is usually uninformative; image processing is out of scope. |
| `output_format="markdown"` | markdown | Structural markers (headings, lists) guide the LLM's sectional understanding. |
| `no_fallback=False` | enabled | Allows the readability fallback extractor for thin or minimal pages. |
| `EXTRACTION_TIMEOUT=0` | 0 | Disables trafilatura's internal timeout, delegating time control entirely to the `PAGE_TIMEOUT_MS` guard at the fetcher level. |

**Truncation** is applied at 15,000 characters (~3,750 tokens at ~4 chars/token)
on the nearest word boundary via `_truncate_on_word_boundary`.  Walking
backwards from the ceiling to the last whitespace character avoids cutting a
token in mid-word.  A trailing ` …` marker signals intentional truncation to
the LLM rather than implying the page content ended abruptly.

---

## 3. `src/llm/` — LLM Extraction Layer

### Module Map

| File | Responsibility |
|---|---|
| `schemas.py` | Pydantic v2 DTOs that double as OpenAI Structured Outputs contracts. |
| `prompts.py` | Versioned, deterministic system and user prompt templates. |
| `client.py` | Thin async OpenAI client wrapper; maps all API errors to `LLMExtractionError`. |

### 3.1 `schemas.py` — Pydantic v2 DTOs

Four `BaseModel` classes form the extraction contract:

```
EnrichedLeadPayload
├── company_name: str
├── analysis: CompanyAnalysis
│   ├── industry: str
│   ├── target_audience: str
│   ├── value_proposition: str
│   └── pain_points: list[str]         (up to 3, taken from the page)
├── scoring: LeadScoring
│   ├── fit_tier: Literal[...]         (4 tier values; JSON schema enum)
│   ├── fit_score: int                 (ge=0, le=100; JSON minimum/maximum)
│   ├── scoring_rationale: str         (chain-of-thought, then one arithmetic line)
│   └── missing_information: list[str] (default []; omitted from JSON required)
└── outreach: OutreachStrategy
    ├── icebreaker: str                (must reference a verifiable page detail)
    └── suggested_angle: str
```

`pain_points` is a `list[str]`.  The field description asks for the top three
problems found on the page.  When the page supports fewer, the list is shorter
and the gap is recorded on `LeadScoring.missing_information`.  A length of
three is requested in that description.  The Pydantic type accepts any list.

`EnrichedLeadPayload.model_json_schema()` is the document the regression suite
treats as the structured-output contract.  Required top-level keys are
`company_name`, `analysis`, `scoring`, and `outreach`.  Nested `$defs` are
exactly `CompanyAnalysis`, `LeadScoring`, and `OutreachStrategy`.
`CompanyAnalysis.required` is `industry`, `target_audience`,
`value_proposition`, `pain_points`.  `OutreachStrategy.required` is
`icebreaker`, `suggested_angle`.  `LeadScoring.required` is `fit_tier`,
`fit_score`, and `scoring_rationale`.  `missing_information` carries
`default_factory=list`, so an omitted key validates as an empty list and the
key is absent from that model's `required` array.  `fit_tier` is a `Literal`
of `Tier 1 (High)`, `Tier 2 (Medium)`, `Tier 3 (Low)`, and `Disqualified`,
which the JSON schema publishes as an enum.  `fit_score` is an `int` with
`ge=0` and `le=100`, published as `minimum` / `maximum`.

All `Field(description=...)` strings are deliberately verbose because the
OpenAI SDK serialises them into the JSON schema injected into the model's system
context.  The field descriptions are therefore functional — they constrain the
model's extraction behaviour, not just document the Python type.

`LeadScoring.fit_score` carries `ge=0, le=100` constraints that are reflected
in the JSON schema (`minimum`/`maximum`), not just enforced post-hoc.  The
`Literal` type on `fit_tier` becomes an `enum` in the JSON schema, preventing
free-form tier labels.

### 3.2 `prompts.py` — ICP Rubric & Prompt Templates

The system prompt encodes the ICP definition, a four-dimension additive
rubric, disqualification rules, and seven evaluator rules in one versioned
constant (`SYSTEM_PROMPT_TEMPLATE`, **v2.0**).  v2.0 replaced the v1.0
heuristic tiers, which collapsed scores toward 85 or 0 and left Tier 3
unreachable.  Data-gap penalties are now local to the dimension the gap
affects.  The module docstring is the changelog.

**ICP definition** (single source of truth, also reflected in the schema
field descriptions):
- Business model: B2B SaaS or tech-enabled B2B services (technology is core
  to delivery, not a thin CRM wrapper).
- Company size: 10–500 employees (growth-stage; Seed through Series C).
  Pre-revenue or bootstrapped SMBs stay in range when employee count and a
  B2B model are confirmed.
- Disqualifiers, applied only to unmistakable non-targets: pure B2C with no
  business-facing offering, NGO/government, staffing or recruitment agencies,
  scam sites, parked domains, and empty pages.  Mixed consumer/business
  audiences, creator tools with a business plan, and open-source projects
  with a paid tier are scored into Tier 2 or Tier 3.  A missing employee
  count or a niche tech-enabled agency lowers the affected dimension and
  leaves the company inside a scored tier.

**Weighted rubric** (100 points, summed into `fit_score`):

| Dimension | Ceiling | Bands the prompt publishes |
|---|---|---|
| A — Business model & value proposition | 35 | 30–35 pure B2B SaaS; 15–25 tech-enabled B2B service; 0 incompatible model |
| B — Target market & ICP relevance | 25 | 20–25 teams / SMB / mid-market; 10–15 ambiguous audience; 0–5 consumer or mega-enterprise |
| C — Commercial clarity & pricing | 20 | 15–20 public tiers or self-serve; 8–12 demo / contact-sales only; 0–5 no commercial intent |
| D — Technical & social proof | 20 | 15–20 named case studies, API, or quantified ROI; 8–14 generic proof; 0–5 none |

**FIT TIER MAPPING** (derived from the total; the prompt forbids setting the
tier independently of `fit_score`):

| Tier | Inclusive band |
|---|---|
| Tier 1 (High) | 75–100 |
| Tier 2 (Medium) | 50–74 |
| Tier 3 (Low) | 25–49 |
| Disqualified | 0–24 |

An incompatible model scores 0 on dimension A and sets `fit_tier` to
`Disqualified`.  The offline suite parses these four lines out of
`SYSTEM_PROMPT_TEMPLATE` and requires them to match its oracle, so a band
edit and the tests move together.  See D-06.

**Evaluator rules** that the golden cases exercise:
1. Facts only — every field comes from the provided page text.
2. Absent data goes to `missing_information` rather than a guessed value.
3. Data-gap penalties are dimension-local: missing pricing reduces C, missing
   employee count reduces B.  Penalties stay inside the affected dimension.
   A well-evidenced B2B SaaS page missing only headcount can still score 65–70.
4. Chain-of-thought before the score.  `scoring_rationale` lists each
   dimension, the awarded points, and a one-sentence justification, and ends
   with exactly one arithmetic line (`A=28, B=18, C=12, D=14 → total=72` is
   the prompt's example shape).  `fit_score` and `fit_tier` are assigned
   after that line.
5. Tier–score consistency.  The prompt calls a mismatch a hard error.  The
   regression suite treats it as a failed case.
6. `icebreaker` cites at least one named detail from the page (product,
   customer, metric, or feature).
7. `company_name` is the trading name taken from the page (title, logo, or
   About).

`build_user_prompt` wraps the URL and Markdown in a structured fence
(`--- BEGIN PAGE CONTENT ---` / `--- END PAGE CONTENT ---`) so the model's
context boundary is unambiguous.  The Markdown argument is the Trafilatura
output, already truncated to the scraper character budget.

### 3.3 `client.py` — OpenAI Structured Outputs Client

`LLMClient` uses `client.beta.chat.completions.parse` exclusively — the
OpenAI SDK method that validates the model response against a Pydantic model
before returning.  The entire class contains zero calls to `json.loads()`,
`json.dumps()`, or dict-to-model coercion.

**Why Structured Outputs over raw JSON parsing**:

| Approach | Risk |
|---|---|
| `json.loads()` on raw model output | Model may emit malformed JSON, trailing commas, code fences, or extra prose; requires defensive parsing and schema re-validation. |
| `response_format={"type": "json_object"}` | Schema is not enforced; model selects field names and types freely; requires a separate Pydantic parse step. |
| `client.beta.chat.completions.parse` with `EnrichedLeadPayload` | Schema is injected into the API call; the SDK validates and constructs the Pydantic model atomically; `parsed` is `None` only on content-filter refusals. |

Temperature is set to `0.1` (not `0.0`) to preserve variety in free-text
fields (`scoring_rationale`, `icebreaker`) while keeping numeric fields
(`fit_score`) near-deterministic.

All `openai.APIError` subclasses (auth, rate-limit, network, content-filter)
are caught and re-raised as `LLMExtractionError`, insulating the orchestration
layer from the OpenAI SDK's exception hierarchy.

---

## 4. `src/storage/` — Async Persistence Layer

### Module Map

| File | Responsibility |
|---|---|
| `database.py` | Schema DDL, idempotent `init_db`, `get_db_connection` context manager. |
| `repository.py` | CRUD operations against the `leads` table; the only SQL-issuing module. |

### 4.1 `database.py` — Schema & Connection Management

**Schema** (`leads` table):

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER PK AUTOINCREMENT` | Internal surrogate key. |
| `url` | `TEXT NOT NULL UNIQUE` | Deduplication key; enforces one record per URL. |
| `domain` | `TEXT NOT NULL` | Extracted `netloc`; indexed for domain-level analytics. |
| `company_name` | `TEXT` | Denormalised from `enriched_data` for fast SQL queries. |
| `fit_score` | `INTEGER` | Denormalised; indexed for score-range queries. |
| `fit_tier` | `TEXT` | Categorical tier label. |
| `enriched_data` | `JSON` | Full `EnrichedLeadPayload.model_dump_json()` artifact. |
| `raw_markdown` | `TEXT` | Archived scraper output for audit and pipeline replay. |
| `status` | `TEXT CHECK(...)` | Lifecycle FSM: `PENDING` → `PROCESSED` \| `FAILED`. |
| `error_message` | `TEXT` | Failure reason; `NULL` on `PROCESSED` records. |
| `created_at` / `updated_at` | `TIMESTAMP` | `CURRENT_TIMESTAMP` defaults. |

Indexes on `domain` and `fit_score` allow efficient filtering without
full-table scans on both the most common analytical query patterns.

`get_db_connection` is an `@asynccontextmanager` that issues
`PRAGMA foreign_keys = ON` and sets `conn.row_factory = aiosqlite.Row`
immediately after opening.  `aiosqlite.Row` supports dict-style access and
`dict()` conversion, keeping callers free of `aiosqlite` internals.

### 4.2 `repository.py` — CRUD & Status Lifecycle

**`insert_pending_lead`** uses `INSERT OR IGNORE` to safely handle concurrent
pipeline tasks that race to insert the same URL.  The subsequent `SELECT id`
retrieves the authoritative PK regardless of whether the `INSERT` fired.  This
is preferable to an `INSERT OR REPLACE` (which would reset `created_at` and
overwrite any existing `enriched_data`) and to a `SELECT` + conditional
`INSERT` (which has a TOCTOU race window).

**Status lifecycle**:

```
[New URL]
    │
    ▼
 PENDING   ← insert_pending_lead()
    │
    ├─── scrape & enrich succeed ──► PROCESSED  ← save_lead_success()
    │
    └─── ScrapeError / LLMExtractionError ──► FAILED  ← mark_lead_failed()
```

`mark_lead_failed` is explicitly designed to never raise; any exception during
failure-state persistence would shadow the original pipeline error, losing the
root-cause context.  The method logs at `WARNING` level and returns silently on
any internal error.

Each repository method opens its own connection via `get_db_connection`.  This
avoids shared-connection lock contention in concurrent pipelines and ensures
all connections are closed even when exceptions propagate.  `aiosqlite` does
not auto-commit DML; every mutating method calls `await conn.commit()`
explicitly before the context exits.

---

## 5. `src/main.py` — CLI Orchestrator

### Execution Model

The pipeline for each URL executes five sequential stages inside
`_process_url`:

1. **Idempotency check** (`LeadRepository.get_lead_by_url`) — runs *before*
   semaphore acquisition so that already-processed URLs consume zero
   concurrency slots.
2. **`insert_pending_lead`** — establishes the `lead_id` used by all
   subsequent stages.  Idempotent via `INSERT OR IGNORE`.
3. **`fetch_page_markdown`** — headless browser → trafilatura → clean Markdown.
4. **`LLMClient.enrich_lead`** — Markdown → validated `EnrichedLeadPayload`.
5. **`LeadRepository.save_lead_success`** — persist structured payload.

On `ScrapeError` or `LLMExtractionError`: `mark_lead_failed` is called (best-
effort, wrapped in the same except block) and the pipeline returns `"failed"`
without re-raising.  `asyncio.gather` therefore always collects all results;
one failing URL does not cancel sibling tasks.

### Concurrency Model

```python
semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_SCRAPES)
tasks = [asyncio.create_task(_process_url(url, ..., semaphore=semaphore)) for url in urls]
results = await asyncio.gather(*tasks)
```

All tasks are created immediately (fan-out), but each acquires `semaphore`
before launching Playwright.  This means:
- At most `MAX_CONCURRENT_SCRAPES` browser contexts are live simultaneously.
- At most `MAX_CONCURRENT_SCRAPES` concurrent OpenAI requests are in flight.
- URLs whose semaphore wait is long (due to slow scrapers ahead) benefit from
  the idempotency check pre-empting the wait for already-processed URLs.

### Error Handling Philosophy

The orchestrator uses a `Literal["processed", "skipped", "failed"]` return
type rather than bubbling exceptions out of `_process_url`.  This enforces
that a per-URL failure is a *handled* outcome, not an unhandled exception.
The `--force` flag bypasses the `PROCESSED` skip guard and re-runs the full
pipeline against all supplied URLs regardless of prior state.

---

## 6. `src/core/exceptions.py` — Exception Hierarchy

```
LeadEnrichmentError (base)
├── ScrapeError(url, reason)          — Playwright failures
├── LLMExtractionError(url, reason)   — OpenAI API errors, schema validation failures
└── StorageError                      — Unrecoverable persistence failures
```

Both `ScrapeError` and `LLMExtractionError` carry `url` and `reason`
attributes with identical signatures.  This symmetry is intentional: the
orchestrator's `except (ScrapeError, LLMExtractionError)` block handles both
through the same `str(exc)` → `mark_lead_failed` path, without needing to
inspect the error type at the call site.

Locating exceptions in `src/core/` breaks the circular-import risk that would
arise if, for example, `scraper` imported from `llm` just to raise a common
error type.

---

## 7. Key Architectural Decisions — Decision Log

### D-01: `domcontentloaded` over `networkidle`

**Decision**: Use `wait_until="domcontentloaded"` as the Playwright navigation
wait strategy.

**Rationale**: `networkidle` is unreliable on modern SaaS sites because
analytics beacons, WebSocket connections, and SSE streams keep the network
active indefinitely.  `domcontentloaded` fires as soon as the HTML is parsed,
which is the earliest point at which the semantic text content is available.
Asset-blocking route interception eliminates most inflight requests before the
event fires, making the signal reliable without sacrificing content fidelity.

### D-02: OpenAI Structured Outputs over raw JSON parsing

**Decision**: Use `client.beta.chat.completions.parse` with
`response_format=EnrichedLeadPayload`.

**Rationale**: Raw JSON parsing (`json.loads()` on model output) requires
defensive handling of malformed JSON, code-fence wrappers, trailing commas,
and free-form field names.  A secondary Pydantic parse adds latency and a
second failure mode.  Structured Outputs injects the JSON schema into the API
call, making the model's output structurally guaranteed before the SDK returns.
The only legitimate `None` case for `parsed` is a content-filter refusal —
a distinct, handleable condition.

### D-03: `INSERT OR IGNORE` + subsequent `SELECT` for idempotency

**Decision**: Use `INSERT OR IGNORE` followed by `SELECT id` rather than an
upsert or select-then-insert pattern.

**Rationale**: Eliminates the TOCTOU race condition that exists when two
concurrent coroutines both observe "no row" and both attempt `INSERT`.  The
`UNIQUE` constraint on `url` guarantees exactly one row exists after both
inserts; the follow-up `SELECT` retrieves the winner's PK regardless of which
coroutine's insert actually landed.

### D-04: Per-method connection opening in `LeadRepository`

**Decision**: Each repository method opens its own `aiosqlite` connection
rather than holding a long-lived connection on the `LeadRepository` instance.

**Rationale**: `aiosqlite` wraps each connection in a dedicated thread for
blocking I/O isolation.  A single shared connection would require an explicit
lock around every method to prevent interleaved writes from concurrent
coroutines.  Per-method connections eliminate lock contention at the cost of
connection-open overhead per operation — acceptable for a pipeline that
performs O(1) writes per URL rather than high-frequency OLTP.

### D-05: Denormalised scalar columns alongside `enriched_data` JSON

**Decision**: Store `company_name`, `fit_score`, and `fit_tier` as dedicated
columns in addition to the full `enriched_data` JSON blob.

**Rationale**: SQLite's JSON path extraction (`json_extract`) works but is
slower than a direct column predicate and requires knowledge of the schema
structure in every query.  Denormalised columns allow `WHERE fit_score > 75`,
`ORDER BY fit_score DESC`, and `GROUP BY domain` without JSON path access,
making the database directly queryable by standard SQL tooling.

### D-06: Deterministic Offline Regression Testing for LLM Structured Outputs & Rubric Tiers

**Decision**: Lock the `EnrichedLeadPayload` structured-output contract and the
v2.0 ICP tier bands with an offline pytest suite
(`tests/test_scoring_regression.py`, `tests/test_schemas.py`,
`tests/fixtures/eval/`).  Golden labels are plain dictionaries validated by
`EnrichedLeadPayload.model_validate` — the same Pydantic contract the OpenAI
parse path enforces, exercised on a local dict.  An autouse
fixture replaces `socket.create_connection` so a TCP connect fails the run.
CI (`.github/workflows/ci.yml`) installs `ruff`, `pytest`, and
`pytest-asyncio`, runs `ruff check src/`, then `pytest tests/ -v`, and exports
a mock `OPENAI_API_KEY` that this suite never reads.

**Rationale**: Structured Outputs make a live response schema-shaped, and
temperature `0.1` still leaves `scoring_rationale` and `icebreaker` free to
vary between calls.  A CI job that called OpenAI would flake on sampling,
spend tokens on every push, and notice a moved tier band only after someone
re-read a live score.  The suite splits that risk into checks whose inputs
are files in the repository:

1. **Prompt drift.**  `test_offline_bands_match_published_prompt` parses the
   en-dash tier lines out of `SYSTEM_PROMPT_TEMPLATE` and requires them to
   equal the in-suite oracle: Tier 1 (High) 75–100, Tier 2 (Medium) 50–74,
   Tier 3 (Low) 25–49, Disqualified 0–24.  Inclusive edges (0, 24, 25, 49,
   50, 74, 75, 100) must each map to exactly one tier and pass
   `LeadScoring`.  Every integer from 0 through 100 belongs to exactly one
   band.
2. **Schema contract stability.**  `model_json_schema()` must keep the
   required keys of `EnrichedLeadPayload`, `CompanyAnalysis`, `LeadScoring`,
   and `OutreachStrategy`, the `fit_tier` enum, and `fit_score` as an integer
   with `minimum` 0 and `maximum` 100.  `missing_information` stays out of
   `LeadScoring.required` because the field has a default.  Scores below 0,
   scores above 100, and tier labels outside the `Literal` (`Tier 4 (Ultra)`,
   `high`) raise `ValidationError` with the expected `loc` and `type`.  A
   JSON round-trip (`model_dump_json` → `model_validate_json`) must reproduce
   the golden payload, which is the shape stored in `enriched_data`.
3. **Rubric arithmetic and evidence.**  Each golden rationale contains exactly
   one `A=…, B=…, C=…, D=… → total=…` line.  Sub-scores stay inside the
   dimension ceilings (35 / 25 / 20 / 20), sum to `fit_score`, and sit in the
   business-model band for that archetype.  A Disqualified label awards 0 on
   dimension A.  Company name, pain points, evidence anchors, and the
   icebreaker's cited detail must occur in the fixture page.

A green run spends no OpenAI tokens and performs no provider round-trip.  CI
failures on this suite are contract diffs: a moved band, a renamed field, a
`Literal` change, a fixture whose evidence is missing from the page, or
sub-scores that disagree with `fit_score`.  The live `linear.app` enrichment remains
the end-to-end behavioral check.  This suite is the contract gate in front
of it.

**Tradeoffs**: Golden pages and expected scores are maintained by hand.  The
three fixtures cover a pure B2B SaaS Tier 1 page (Forgeboard, 92), a
tech-enabled consulting Tier 2 page (Harborline Partners, 54), and a B2C
catalog that must be Disqualified (Willow & Grain, 11).  Tier 3 coverage in
this revision is the inclusive edges 25 and 49 together with the full 0–100
partition.  The socket patch guards `socket.create_connection`, which is the
connect path the suite is written to forbid.  The tests construct no
`LLMClient`.  A later HTTP client that connects through a different socket
API would need the same isolation extended in the fixture.  The suite accepts
a label that satisfies the rubric and the schema.  Live model sampling stays
on the manual enrichment path, so a wording change that keeps the bands can
still move a real score until the next live run.

---

## 8. `tests/` — Offline Evaluation Suite

### Module Map

| Path | Responsibility |
|---|---|
| `pytest.ini` | `pythonpath = .` and `testpaths = tests`.  Bare `pytest` imports `src` without an editable install. |
| `test_schemas.py` | Happy-path `EnrichedLeadPayload` construction and rejection of `LeadScoring.fit_score` above 100. |
| `test_scoring_regression.py` | Golden-set rubric checks, band edges, negative schema cases, JSON-schema shape, prompt-band parity, fixture registration. |
| `fixtures/eval/*.md` | Scraped-page stand-ins.  The set of `*.md` names must equal the registered `_EVAL_CASES`. |

### What the 21 tests cover

| Group | Count | Contract under test |
|---|---|---|
| Golden fixtures | 3 | `tier1_saas.md` (92, Tier 1), `tier2_consulting.md` (54, Tier 2), `disqualified_b2c.md` (11, Disqualified).  Grounding, dimension arithmetic, tier band, DTO round-trip. |
| Inclusive band edges | 8 | 100, 75, 74, 50, 49, 25, 24, 0.  Each edge is a valid `LeadScoring` and maps to one tier. |
| Rejected `LeadScoring` bodies | 4 | `fit_score=-1` (`greater_than_equal`), `fit_score=101` (`less_than_equal`), `Tier 4 (Ultra)` and `high` (`literal_error` on `fit_tier`). |
| Contract invariants | 4 | JSON schema shape for the four DTOs; prompt-band parity with `SYSTEM_PROMPT_TEMPLATE`; partition of scores 0–100; on-disk fixtures match registered cases. |
| `test_schemas.py` | 2 | One valid `EnrichedLeadPayload`; `fit_score=150` raises `ValidationError`. |
| **Total** | **21** | |

Current cases are synchronous Pydantic and rubric checks.  CI also installs
`pytest-asyncio` next to `pytest` and `ruff`, so an async test added later
uses the same job.  Ruff is scoped to `ruff check src/`, matching the
workflow.  The decision record for why this suite exists, and what it
deliberately leaves on the live path, is D-06.
