# Architectural State Document — B2B Lead Enrichment & Scoring Agent

> **Purpose**: Captures the current implementation state of every module, the
> rationale behind non-obvious design choices, and the invariants that must be
> preserved as the codebase evolves.  This document is a living record; update
> it whenever a design decision changes.
>
> **Last updated**: 2026-09-17  
> **Pipeline status**: End-to-end verified against live targets (linear.app).

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
│   └── pain_points: List[str]        (exactly 3, verified from page)
├── scoring: LeadScoring
│   ├── fit_tier: Literal[...]         (4 tier values)
│   ├── fit_score: int                 (ge=0, le=100)
│   ├── scoring_rationale: str         (chain-of-thought before score)
│   └── missing_information: List[str] (acknowledged data gaps)
└── outreach: OutreachStrategy
    ├── icebreaker: str                (must reference verifiable page detail)
    └── suggested_angle: str
```

All `Field(description=...)` strings are deliberately verbose because the
OpenAI SDK serialises them into the JSON schema injected into the model's system
context.  The field descriptions are therefore functional — they constrain the
model's extraction behaviour, not just document the Python type.

`LeadScoring.fit_score` carries `ge=0, le=100` constraints that are reflected
in the JSON schema (`minimum`/`maximum`), not just enforced post-hoc.  The
`Literal` type on `fit_tier` becomes an `enum` in the JSON schema, preventing
free-form tier labels.

### 3.2 `prompts.py` — ICP Rubric & Prompt Templates

The system prompt encodes the full ICP definition and seven strict evaluator
rules in a single versioned constant (`SYSTEM_PROMPT_TEMPLATE`, v1.0).

**ICP definition** (single source of truth):
- Business model: B2B SaaS or tech-enabled B2B services.
- Company size: 10–500 employees (growth-stage; Seed through Series C).
- Disqualifiers: B2C, NGO/government, sole traders, pure hardware, staffing agencies, <10 or >500 employees.

**Key rubric rules** that enforce determinism:
1. Facts only — no external knowledge or hallucination.
2. `scoring_rationale` must be populated *before* assigning `fit_score` — the
   prompt explicitly frames the rationale field as the chain-of-thought
   scratchpad.
3. Tier–score consistency is declared a "hard error" in the prompt.
4. Each `missing_information` entry must reduce the `fit_score` — data gaps
   have a quantified cost.

`build_user_prompt` wraps the URL and Markdown in a structured fence
(`--- BEGIN PAGE CONTENT ---` / `--- END PAGE CONTENT ---`) so the model's
context boundary is unambiguous.

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
