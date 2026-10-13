# Architectural State Document — B2B Lead Enrichment & Scoring Agent

> **Purpose**: Captures the current implementation state of every module, the
> rationale behind non-obvious design choices, and the invariants that must be
> preserved as the codebase evolves.  This document is a living record; update
> it whenever a design decision changes.
>
> **Last updated**: 2026-10-14  
> **Version**: 2.0.0 (`pyproject.toml` and `API_VERSION` in `src/api/app.py` agree).  
> **Pipeline status**: CLI and REST API share one `EnrichmentService`.  Storage is SQLite or PostgreSQL, the LLM layer is provider-agnostic through LiteLLM, and browser traffic is SSRF-guarded.  147 offline tests pass (`uv run pytest`); `ruff check src/` and `ruff format --check src/` are clean.  The live `linear.app` run remains the end-to-end behavioural check.
>
> **Reading guide**: decisions are recorded in §7 (D-01 … D-12).  Entries superseded by later decisions are annotated, not deleted, so the history stays auditable.

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
| `LLM_MODEL` | `gpt-4o-mini` | LiteLLM model identifier.  Its prefix selects the provider and therefore the API key (see D-07).  Replaces the v1 `OPENAI_MODEL`. |
| `OPENAI_API_KEY` | `None` | `SecretStr`.  Required only when `LLM_MODEL` resolves to OpenAI. |
| `ANTHROPIC_API_KEY` | `None` | `SecretStr`.  Required only for `claude-*` / `anthropic/...` models. |
| `OPENROUTER_API_KEY` | `None` | `SecretStr`.  Required only for `openrouter/<vendor>/<model>`. |
| `OPENAI_BASE_URL` | `None` | OpenAI-compatible endpoint override.  Applies to OpenAI models only; ignored with a warning otherwise. |
| `LLM_TIMEOUT_S` | `45.0` (`> 0`) | Per-attempt provider timeout. |
| `LLM_MAX_RETRIES` | `2` (`0..5`) | LiteLLM retries on transient provider errors. |
| `DATABASE_PATH` | `leads.db` | SQLite file; used when `DATABASE_URL` is unset. |
| `DATABASE_URL` | `None` | `SecretStr` PostgreSQL DSN.  When set it replaces SQLite (see D-08). |
| `MAX_CONCURRENT_SCRAPES` | `3` | Bound of the `asyncio.Semaphore` shared by CLI and API. |
| `PAGE_TIMEOUT_MS` | `20000` | Playwright navigation timeout; also the scraper's anti-hang guard. |
| `LOG_LEVEL` | `INFO` | Root logging level (`configure_logging` for the API, the Rich handler for the CLI). |

**Migration note.**  `Settings` uses `extra="ignore"`, so a v1 `.env` that
still sets `OPENAI_MODEL` is silently ignored and the default model is used.
Rename it to `LLM_MODEL`.

**Slot-hold bound.**  One LLM call can occupy a concurrency slot (and its
browser) for at most `(LLM_MAX_RETRIES + 1) * LLM_TIMEOUT_S`, which is 135 s
with the defaults.  LiteLLM's own default timeout is 6000 s, which is why the
explicit 45 s value exists.

### Repository selection — `get_repository()`

`get_repository(settings)` returns a `PostgresLeadRepository` when
`DATABASE_URL` is set and a SQLite `LeadRepository` otherwise.  A
`DATABASE_URL` that does not start with `postgresql://` or `postgres://`
raises `ValueError`: falling back to SQLite would silently write to the wrong
store.  The storage imports are deferred into the function because the storage
modules import `src.llm`, which imports `src.config`; a top-level import would
be circular.  The caller owns the lifecycle (`init_db()` first, `close()`
last).

---

## 2. `src/scraper/` — Playwright Scraping Pipeline

### Module Map

| File | Responsibility |
|---|---|
| `browser.py` | Chromium lifecycle, request filter, destination policy, CDP redirect guard. |
| `fetcher.py` | Page navigation, wait strategy, error mapping, DOM extraction. |
| `cleaner.py` | HTML → Markdown normalisation via trafilatura, token-budget enforcement. |

### 2.1 `browser.py` — Chromium Lifecycle

`BrowserManager` is an `async with`-compatible context manager that owns
exactly one Playwright instance, one Chromium browser, and one browser context.
Teardown (`_teardown`) closes the browser (which also closes its contexts and
pages) and then stops the Playwright driver, in independent try/finally blocks
so the driver subprocess, the process that would otherwise outlive us, is
always stopped even if the browser close raises or is cancelled.  The same
routine runs when `__aenter__` fails midway (it catches `BaseException`),
because `__aexit__` never runs for a failed `__aenter__`.

**Request filtering** is registered at the context level (not per-page):

```python
await self._context.route("**/*", _RequestFilter(self._policy))
```

`_RequestFilter` is a callable class with three outcomes per request:

| Request | Outcome |
|---|---|
| `image`, `media`, `font`, `stylesheet` | `route.abort()`; the guard is not consulted. |
| Destination allowed by `_DestinationPolicy` | `route.continue_()`. |
| Destination denied | `route.abort("blockedbyclient")`. |

Context-level registration means every page opened inside the context
inherits the filter automatically.  Unsafe requests are *aborted*, never
fulfilled, so the page sees a network error rather than attacker-shaped
content.

**`_DestinationPolicy`** wraps an injectable `UrlGuard` (default
`assert_public_destination` from `src/core/url_safety.py`).  Verdicts are
cached per `scheme://host:port` for the lifetime of the context, so a page
issuing hundreds of same-origin requests costs one DNS lookup.  `data:`,
`blob:` and `about:` URLs bypass the guard because they have no host.  The
policy **fails closed**: an `UnsafeUrlError` and an unexpected guard crash
both deny the request (the crash is logged with `logger.exception`).

**Redirect interception.**  Playwright's route handler does not see redirect
hops: Chromium follows a 3xx internally and Playwright reports it only
afterwards as a passive `request` event.  `BrowserManager.new_page()` is the
only way to obtain a page, and it attaches a `_RedirectGuard` before the
caller can navigate.  The guard opens a CDP session and enables
`Fetch.enable` with a `Response`-stage pattern.  For every paused response:

- a 3xx with a `Location` header: the header is resolved against the request
  URL (`urljoin`) and vetted through the shared `_DestinationPolicy`; a denied
  target gets `Fetch.failRequest(BlockedByClient)`, otherwise
  `Fetch.continueResponse`;
- anything else: `Fetch.continueResponse` without a lookup;
- any internal error: `Fetch.failRequest`, because a paused request that is
  never answered would hang the navigation.  If even that fails (target
  already gone) it is logged at DEBUG.

Handler tasks are held in a set because the event loop keeps only weak
references to tasks.  See D-09 for the rationale and the residual limits.

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

Playwright's own messages name the exact address and network error, so they go
only into `ScrapeError.reason` (logs).  `public_reason`, which is the only text
persisted and served, is one of a few fixed strings: the navigation timeout,
`"URL blocked: destination is not a public address"` (when the message contains
`ERR_BLOCKED_BY_CLIENT`), `"Page could not be loaded"`, or
`"Page content could not be read"`.  See D-10.

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
| `schemas.py` | Pydantic v2 DTOs that double as the structured-output `response_format` contract. |
| `prompts.py` | Versioned, deterministic system and user prompt templates. |
| `client.py` | Provider-agnostic async LiteLLM wrapper; maps every provider and validation error to `LLMExtractionError`. |

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

All `Field(description=...)` strings are deliberately verbose because LiteLLM
serialises them into the JSON schema sent to the provider as the response
format.  The field descriptions are therefore functional — they constrain the
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

### 3.3 `client.py` — LiteLLM Structured Outputs Client

`LLMClient` routes every call through `litellm.acompletion` with
`response_format=EnrichedLeadPayload` and validates the returned content with
`EnrichedLeadPayload.model_validate_json`.  The module contains no
`json.loads()` and no dict-to-model coercion (architectural invariant 3).  See
D-07 for why the OpenAI-only `.parse` call was replaced.

**Provider and key resolution.**  `_resolve_provider(LLM_MODEL)` is an
allowlist, not a default:

| `LLM_MODEL` shape | Provider | Key setting |
|---|---|---|
| `gpt-*`, `chatgpt-*`, `ft:gpt-*`, `o1*`, `o3*`, `o4*`, or `openai/<model>` | `openai` | `OPENAI_API_KEY` |
| `claude*` or `anthropic/<model>` | `anthropic` | `ANTHROPIC_API_KEY` |
| `openrouter/<vendor>/<model>` | `openrouter` | `OPENROUTER_API_KEY` |
| anything else (`gemini/...`, `azure/...`, ...) | rejected with `ValueError` | n/a |

`LLMClient.__init__` resolves the key and raises `ValueError` if the one it
needs is unset, so a misconfiguration fails at startup rather than once per
lead.  Keys are passed to LiteLLM explicitly because values loaded from `.env`
by pydantic-settings are not exported to `os.environ`, where LiteLLM would
otherwise look.

**Call parameters.**  `timeout=LLM_TIMEOUT_S`, `num_retries=LLM_MAX_RETRIES`,
`temperature=0.1`, and `api_base` only when `OPENAI_BASE_URL` is set *and* the
model is an OpenAI model.  With any other provider the override is dropped and
a warning is logged, so a proxy URL can never receive another vendor's key.
There is **no fallback routing**: no `litellm.Router`, no `fallbacks`.
Resilience is retries on the one configured model.

**Module-level LiteLLM hardening.**

- `LITELLM_LOCAL_MODEL_COST_MAP=True` is set via `os.environ.setdefault`
  *before* `import litellm`.  By default LiteLLM downloads its cost map from
  GitHub at import, which would open a socket (tripping the offline test guard)
  and add a startup network dependency in production.
- `litellm.telemetry = False`.
- `litellm.drop_params = True` discards request parameters a provider does not
  support (for example `temperature` on reasoning models) instead of failing.
- The `LiteLLM*`, `httpx`, `httpcore` and `openai` loggers are pinned to
  `WARNING`, so `LOG_LEVEL=DEBUG` for the application never dumps provider
  traffic and headers.

**Error mapping.**  LiteLLM maps every provider failure (auth, rate limit,
context window, timeout, connection, content policy) onto subclasses of
`openai.OpenAIError`; one `except` clause therefore covers all providers.  The
`openai` package stays a dependency for that exception hierarchy only.  Three
failure paths raise `LLMExtractionError`, each with a fixed `public_reason`:

| Cause | `public_reason` |
|---|---|
| `openai.OpenAIError` from LiteLLM | `"LLM provider request failed (<ExceptionType>)"` |
| Empty `message.content` (possible refusal) | `"LLM returned an empty response"` |
| `pydantic.ValidationError` on the content | `"LLM response did not match the expected schema"` |

The full provider or validation text goes to `reason` and the error log only.

Temperature is `0.1` (not `0.0`) to preserve variety in free-text fields
(`scoring_rationale`, `icebreaker`) while keeping `fit_score` near
deterministic.

---

## 4. `src/storage/` — Async Persistence Layer

### Module Map

| File | Responsibility |
|---|---|
| `base.py` | `BaseLeadRepository` ABC: the only storage surface the service, CLI and API depend on. |
| `database.py` | SQLite schema DDL, idempotent `init_db`, `get_db_connection` context manager. |
| `repository.py` | SQLite driver (`LeadRepository`). |
| `postgres.py` | PostgreSQL driver (`PostgresLeadRepository`) on an `asyncpg` pool. |

Each driver owns its SQL and dialect differences; no other module issues SQL.
The backend is chosen by `get_repository()` (§1).  The SQL in 4.1 and 4.2 is
the SQLite driver; 4.3 and 4.4 cover the contract and the PostgreSQL driver.

### 4.1 `database.py` — SQLite Schema & Connection Management

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

### 4.2 `repository.py` — SQLite CRUD & Status Lifecycle

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
    ├─── ScrapeError / LLMExtractionError / unexpected error ──► FAILED  ← mark_lead_failed()
    │
    └─── re-submission (FAILED, or PROCESSED with force) ──► PENDING  ← reset_to_pending()
```

`mark_lead_failed` lets database errors propagate; the driver does not
swallow them.  Protection against masking the original pipeline error lives
one level up: `EnrichmentService._record_failure` wraps the call, logs with
`logger.exception`, and returns (§5.1).  (An earlier revision of this document
claimed the repository method itself never raises; that did not match the
code.)  The driver logs the transition at `WARNING`.

Each repository method opens its own connection via `get_db_connection`.  This
avoids shared-connection lock contention in concurrent pipelines and ensures
all connections are closed even when exceptions propagate.  `aiosqlite` does
not auto-commit DML; every mutating method calls `await conn.commit()`
explicitly before the context exits.

---

### 4.3 `base.py` — `BaseLeadRepository` contract

| Method | Contract |
|---|---|
| `init_db()` | Idempotent; awaited once before any other call.  Creates the schema (and, for Postgres, the pool). |
| `close()` | Releases what `init_db` acquired.  Non-abstract no-op, inherited by SQLite (no held resources). |
| `get_lead_by_url(url)` | Full row (including `raw_markdown`) or `None`; used by the idempotency check. |
| `get_lead_by_id(lead_id)` | API-facing row: every column except `raw_markdown`, which is too large for responses. |
| `list_leads(skip, limit, fit_tier, status)` | `(rows, total)`, newest first (`ORDER BY id DESC`); exact-match filters; `total` ignores paging. |
| `ping()` | `True` when reachable; connectivity failures propagate so callers can log the cause. |
| `insert_pending_lead(url, domain)` | Creates a `PENDING` row or returns the existing ID; safe under concurrent calls (D-03). |
| `reset_to_pending(lead_id)` | Sets `PENDING` and clears `error_message`; keeps old `enriched_data` until the re-run overwrites it. |
| `save_lead_success(lead_id, payload, raw_markdown)` | Persists the validated `EnrichedLeadPayload` and sets `PROCESSED`. |
| `mark_lead_failed(lead_id, error_message)` | Sets `FAILED` and records the sanitised message. |
| `get_leads_by_urls(urls)` | Summary rows for a CLI batch, `fit_score` descending, unscored rows last. |

### 4.4 `postgres.py` — PostgreSQL driver

`PostgresLeadRepository(dsn, min_size=1, max_size=10)` shares one
`asyncpg.Pool` across all coroutines.  The pool is created lazily in
`init_db()` and released in `close()`; any call before `init_db()` raises
`RuntimeError`.

The DDL mirrors the SQLite schema with native types: `id BIGINT GENERATED
ALWAYS AS IDENTITY`, `enriched_data JSONB`, `created_at` / `updated_at`
`TIMESTAMPTZ NOT NULL DEFAULT now()`, the same `status` CHECK constraint, and
the same two indexes.  `init_db()` executes the multi-statement DDL in one
call (permitted because no parameters are bound).

Dialect differences from the SQLite driver:

| Concern | SQLite | PostgreSQL |
|---|---|---|
| Insert-if-absent | `INSERT OR IGNORE` + `SELECT id` | `INSERT ... ON CONFLICT (url) DO NOTHING RETURNING id`, then `SELECT id` when `RETURNING` yields nothing |
| Parameters | `?` | `$1 ... $n` |
| JSON column | `JSON` (text) | `JSONB`; written as `$n::jsonb` from `payload.model_dump_json()` |
| Batch lookup | chunked `IN (...)` (500 per statement) | `url = ANY($1::text[])`, `ORDER BY fit_score DESC NULLS LAST` |
| Connections | one per call | pooled |

Interpolated SQL is limited to fixed fragments (the column list, `WHERE`
clauses built from a fixed set, placeholder numbers); every value is bound.

**Test status.**  The offline suite exercises the API and service against the
SQLite driver; **no offline test targets `PostgresLeadRepository`**, because
the suite forbids sockets.  The Postgres path is verified by running the
Docker Compose stack.  A containerised integration test is the natural next
step and is listed in §9.

---

## 5. Service Layer & Entry Points

Both entry points drive the same `EnrichmentService`, so CLI and API have
identical semantics.  See D-12.

### 5.1 `src/services/enrichment.py` — `EnrichmentService`

The pipeline is split into two steps so the API can answer before the work
finishes:

1. **`submit(url, force=False)`** runs *before* the semaphore is taken, so
   already-processed URLs consume no concurrency slot.  It calls
   `get_lead_by_url`; if the row is `PROCESSED` and `force` is false it
   returns `Submission(needs_processing=False)`.  Otherwise it calls
   `insert_pending_lead` and, when a row already existed, `reset_to_pending`,
   so a `FAILED` or forced lead does not keep reporting its old outcome to
   pollers while the re-run is in flight.
2. **`process_lead(lead_id, url)`** guards on an in-process `_in_flight` set
   (a repeated request for the same lead returns `"skipped"` instead of
   starting a second pipeline), then runs `scrape -> LLM -> persist` under
   `async with semaphore`.

`process_lead` never raises for pipeline failures, because in a background
task nobody awaits the result and an escaped exception would leave the lead
`PENDING` forever:

| Exception | Stored `error_message` | Logged |
|---|---|---|
| `ScrapeError`, `LLMExtractionError` | `exc.public_reason` | `logger.error` with the full text |
| any other `Exception` | `"Unexpected error: <ExceptionType>"` | `logger.exception` (full traceback) |

Failure recording goes through `_record_failure`, which logs and swallows its
own errors so persistence trouble never masks the original failure.
`enrich_url` (`submit` then `process_lead`) is the CLI's per-URL entry point.
The scraper is injected (`Scraper = Callable[[str, int], Awaitable[str]]`) so
tests never open a browser.

**Concurrency.**  One `asyncio.Semaphore(MAX_CONCURRENT_SCRAPES)` bounds live
browser contexts and simultaneous LLM calls across all in-flight leads.
`_in_flight` is per-process state.

### 5.2 `src/api/` — REST API

| File | Responsibility |
|---|---|
| `app.py` | `create_app(settings, *, llm_client, scraper)` factory and lifespan; module-level `app = create_app()`; `API_VERSION = "2.0.0"`. |
| `routes.py` | Thin adapters over `EnrichmentService` and the repository. |
| `schemas.py` | Request and response DTOs. |
| `security.py` | Re-exports `src.core.url_safety`; the scraper may not import from the API layer. |

**Lifespan.**  Configure logging, build the repository via
`get_repository`, `await repo.init_db()`, construct the `EnrichmentService`
and store `repo` and `service` on `app.state`; on shutdown `await
repo.close()`.  `create_app` accepts an injected `LLMClient` and scraper so
tests need no provider key and no browser.

**Endpoints.**

| Method and path | Success | Errors |
|---|---|---|
| `POST /api/v1/enrich` | `202` `EnrichResponse` (queued); `200` when the URL is already `PROCESSED` and `force` is false | `422` unsafe or malformed URL |
| `GET /api/v1/leads/{lead_id}` | `200` `LeadResponse` | `404` unknown id |
| `GET /api/v1/leads` | `200` `LeadListResponse`; query `page` (>= 1), `page_size` (1..100, default 50), `fit_tier`, `status` | `422` invalid query |
| `GET /health` | `200` `{"status":"healthy","database":"ok"}` | `503` `{"status":"unhealthy","database":"unreachable"}` |

FastAPI also serves `/docs` and `/openapi.json`.

**The 200/202 contract.**  `POST /enrich` validates, calls `submit`, and either
answers `200` immediately (nothing to do) or registers
`service.process_lead` with FastAPI `BackgroundTasks` and answers `202`.  The
client polls `GET /api/v1/leads/{lead_id}` until `status` is `PROCESSED` or
`FAILED`.

**URL validation order.**  `EnrichRequest.url` runs `validate_url_syntax` in a
Pydantic field validator (no I/O); the route then runs `assert_public_host`
(DNS) and maps `UnsafeUrlError` to `422`.  `url` stays a plain `str` rather
than `HttpUrl` because `HttpUrl` would rewrite `https://linear.app` to
`https://linear.app/` and fork the identity key shared with the CLI.

**Response DTOs.**  `LeadResponse` parses stored `enriched_data` text into a
dict with a `TypeAdapter(dict[str, Any]).validate_json` (no hand-rolled
`json.loads`), because both drivers return JSON text.  `LeadStatus` and
`FitTier` are `Literal` aliases.

**Health.**  A failing `ping()` is logged with `logger.exception` and reported
as `503`, never surfaced as a `500`.

**Known limits.**  The API has **no authentication**; Docker Compose binds it
to loopback and assumes an upstream gateway or reverse proxy for any wider
exposure.  Background tasks run in the API process, so a restart mid-job
leaves that lead `PENDING`; there is no recovery sweep, and re-submitting the
URL re-queues it.  `_in_flight` and the semaphore are per-process, so running
several API workers multiplies the effective concurrency.

### 5.3 `src/main.py` — CLI

`main()` parses arguments, builds the repository, awaits `init_db()`, and runs
`_run`, which creates one `EnrichmentService` and fans out
`asyncio.create_task(service.enrich_url(url, force=force))` for every URL.
`asyncio.gather` always collects all results, because a per-URL failure is a
*handled* outcome (`Literal["processed", "skipped", "failed"]`), not an
exception.  `repo.close()` runs in a `finally`.  `--force` bypasses the
`PROCESSED` skip.

Presentation is Rich only: logs go to stderr through `RichHandler`; the run
summary panel and the per-URL results table (from `get_leads_by_urls`, ordered
by score, tier-coloured) go to stdout so output can be piped.

---

## 6. `src/core/` — Exceptions and URL Safety

### 6.1 `exceptions.py` — Exception hierarchy

```
LeadEnrichmentError (base; public_reason = "Enrichment failed")
├── ScrapeError(url, reason, *, public_reason="Page could not be retrieved")
├── LLMExtractionError(url, reason, *, public_reason="LLM enrichment failed")
└── StorageError
```

`reason` is the full diagnostic for server-side logs and may contain driver or
vendor detail.  `public_reason` is the fixed, caller-safe summary persisted on
the lead and served by the API.  The two classes carry identical attributes,
so the service handles both through one `except (ScrapeError,
LLMExtractionError)` and stores `exc.public_reason` without inspecting the
type.  Locating exceptions in `src/core/` avoids the circular imports that
would arise if, for example, `scraper` imported from `llm` to raise a common
type.  `StorageError` is defined but nothing raises it today.

### 6.2 `url_safety.py` — SSRF primitives

The scraper drives a real browser at whatever URL it is given, so an untrusted
caller could otherwise aim it at loopback services, the cloud metadata
endpoint (`169.254.169.254`) or the internal network.  The module is in
`core` because both the API and the scraper enforce the same policy.

| Function | Role |
|---|---|
| `validate_url_syntax(url)` | Pure, no I/O.  Rejects empty or over-2048-character URLs, whitespace, control characters and backslashes (browsers treat `\` as `/`, so `http://127.0.0.1\@example.com` would otherwise pass), malformed ports, non-http(s) schemes, blocked hostnames (`localhost`, `*.local`, `*.internal`, `*.localhost`, `*.localdomain`, `*.home.arpa`) and non-public IP literals. |
| `assert_public_host(url, resolver)` | Resolves non-literal hostnames (5 s timeout) and rejects the URL if **any** resolved address is non-public, because the browser may pick any of them.  Catches obfuscated forms such as `2130706433` or `0x7f.1`, which only reveal their target once resolved. |
| `assert_public_destination(url, resolver)` | One browser request: scheme, hostname checks and resolved addresses.  Does not cap length or reject odd characters, since sub-resource URLs are legitimately long and Chromium has already normalised them. |

`_is_public_ip` uses `ip.is_global and not ip.is_multicast` and unwraps
IPv4-mapped IPv6 (`::ffff:127.0.0.1`) before judging.  The resolver is an
injectable `HostResolver`, so tests never touch DNS.  See D-09.

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

> **Status: superseded by D-07 (v2.0).**  The principle stands (schema-constrained
> output, no `json.loads`, Pydantic as the only parser).  The mechanism changed:
> `client.beta.chat.completions.parse` is gone, replaced by LiteLLM with
> `response_format=EnrichedLeadPayload` and `model_validate_json`.  The text
> below is kept as the v1 record.

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

> **Amended in v2.0 (D-08):** the PostgreSQL driver applies the same pattern in
> its own dialect: `INSERT ... ON CONFLICT (url) DO NOTHING RETURNING id`, then
> `SELECT id` when `RETURNING` yields nothing.  The invariant (a `UNIQUE` `url`
> plus insert-if-absent plus read-back, never select-then-insert) is unchanged.

**Decision**: Use `INSERT OR IGNORE` followed by `SELECT id` rather than an
upsert or select-then-insert pattern.

**Rationale**: Eliminates the TOCTOU race condition that exists when two
concurrent coroutines both observe "no row" and both attempt `INSERT`.  The
`UNIQUE` constraint on `url` guarantees exactly one row exists after both
inserts; the follow-up `SELECT` retrieves the winner's PK regardless of which
coroutine's insert actually landed.

### D-04: Per-method connection opening in `LeadRepository`

> **Scope narrowed in v2.0 (D-08):** this applies to the **SQLite** driver only.
> `PostgresLeadRepository` shares one `asyncpg` pool (1 to 10 connections), which
> gives the same freedom from a shared-connection lock without a connect per call.

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
`EnrichedLeadPayload.model_validate` — the same Pydantic contract the
production LLM path enforces (`model_validate_json`), exercised on a local
dict.  Autouse fixtures replace `socket.create_connection` (in the module) and,
suite-wide in `tests/conftest.py`, `socket.create_connection`,
`socket.socket.connect` and `socket.getaddrinfo`, so a connect or DNS lookup
fails the run.  CI (`.github/workflows/ci.yml`) installs with `uv sync --dev`,
runs `ruff check src/`, then `pytest tests/ -v`, and exports a mock
`OPENAI_API_KEY` that the suite never uses to reach a provider.

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

### D-07: LiteLLM as the provider-agnostic LLM layer

**Decision**: Replace the OpenAI-only `client.beta.chat.completions.parse` call
with `litellm.acompletion(..., response_format=EnrichedLeadPayload)`, validate
the returned content with `EnrichedLeadPayload.model_validate_json`, and select
the provider with a single `LLM_MODEL` setting.  Supersedes D-02's mechanism.

**Rationale**: The ICP scoring contract is vendor-neutral; the client was not.
LiteLLM gives one call shape across OpenAI, Anthropic and OpenRouter, so
changing provider is configuration, not code.  The invariant behind D-02
survives: output is schema-constrained at the provider and parsed exactly once,
by Pydantic.  Hardening choices that came with the migration:

1. **Allowlist, not default.**  LiteLLM will route `gemini/...` or `azure/...`
   to that vendor.  The only credential this application could attach would be
   the OpenAI one, so unsupported prefixes are rejected at construction
   instead of defaulted to OpenAI.
2. **Fail fast on keys.**  The key required by `LLM_MODEL` is checked in
   `LLMClient.__init__`; a missing key surfaces at startup, not once per lead.
   Keys are passed explicitly because `.env` values are not in `os.environ`.
3. **Bounded waits.**  `LLM_TIMEOUT_S` (45) and `LLM_MAX_RETRIES` (2) cap a
   call at 135 s of slot time; LiteLLM's own default timeout is 6000 s.
4. **No startup network dependency.**  `LITELLM_LOCAL_MODEL_COST_MAP=True` is
   set before the import, so LiteLLM never fetches its cost map from GitHub.
   This also keeps the offline test guard intact.
5. **Quiet and portable.**  Telemetry off; `drop_params=True` for providers
   that reject `temperature`; provider and HTTP loggers pinned to `WARNING`.
6. **Base URL cannot leak a key.**  `OPENAI_BASE_URL` applies to OpenAI models
   only; with other providers it is ignored with a warning.

**Tradeoffs**: Retry-only resilience; there is **no fallback routing** (no
`litellm.Router`), so a provider outage fails the lead (recorded `FAILED`, safe
to re-submit).  Structured-output support varies by model, and `drop_params`
can hide an unsupported parameter, so a new model should be checked with a live
run.  The `openai` package remains a dependency solely for the exception
hierarchy LiteLLM maps onto.

### D-08: PostgreSQL alongside SQLite behind one repository contract

**Decision**: Introduce `BaseLeadRepository` (`src/storage/base.py`) and a
second driver, `PostgresLeadRepository` on an `asyncpg` pool.  `DATABASE_URL`
selects PostgreSQL; otherwise SQLite on `DATABASE_PATH` is used.

**Rationale**: SQLite keeps the CLI zero-setup, but a concurrent HTTP API and a
multi-container deployment need a server database.  An ABC lets the service,
CLI and API stay unchanged; each driver owns its SQL.  The two schemas are
deliberately parallel (same columns, constraint and indexes), with native
types on Postgres (`JSONB`, `TIMESTAMPTZ`, `BIGINT IDENTITY`).  Idempotency
(D-03) is re-expressed as `ON CONFLICT DO NOTHING RETURNING`.  `init_db()` is
idempotent and also builds the pool, and `close()` releases it.  A non-Postgres
`DATABASE_URL` raises instead of falling back to SQLite, so a typo cannot
silently redirect writes to a local file.  The DSN is a `SecretStr`.

**Tradeoffs**: Two drivers to keep in step (any new contract method must be
implemented twice).  No schema migration tool: `CREATE ... IF NOT EXISTS` only,
so column changes need a manual migration.  **The Postgres driver has no
offline test**, because the suite forbids sockets; it is exercised through the
Docker Compose stack.

### D-09: Two-layer SSRF guard, including CDP redirect interception

**Decision**: Enforce one URL policy (`src/core/url_safety.py`) at two points:
the API front door (syntax in a Pydantic validator, then DNS resolution in the
route) and, inside the scraper, on every browser request.  Redirect hops are
vetted by a per-page CDP `Fetch` interceptor at the `Response` stage.

**Rationale**: The service points a real browser at caller-supplied URLs, so
loopback, the cloud metadata endpoint and internal ranges must be unreachable.
A front-door check alone fails to the classic bypass: a public URL that 302s
to `169.254.169.254`.  Playwright's route handler cannot close that gap,
because Chromium follows a 3xx internally and Playwright reports it only
afterwards.  The CDP interceptor pauses each response, resolves `Location`
against the request URL, and fails the request before Chromium follows it.
Supporting choices:

- The module lives in `core`, not `api`, because the scraper may not import the
  API layer (invariant 2).
- Verdicts are cached per origin, so same-origin sub-requests cost one lookup.
- **Fail closed everywhere**: a failed lookup, a crashing guard, or an error
  in the redirect handler denies the request.  A paused request must always be
  answered or the page hangs, so the handler fails the request on any error.
- Unsafe requests are aborted (`blockedbyclient`), never fulfilled with
  substitute content.
- Every resolved address is checked, IPv4-mapped IPv6 is unwrapped, and
  obfuscated literals (`2130706433`, `0x7f.1`) are caught after resolution.

**Residual limits (stated, not hidden)**: Chromium resolves the name again when
it connects, so DNS rebinding between our lookup and its own is not closed;
popups and cross-site iframes run in targets without the redirect interceptor.
This is a defence in depth, not a replacement for egress filtering on the
scraper host.

### D-10: Sanitised public errors; full detail only in logs

**Decision**: Every domain exception carries two strings.  `reason` is the full
diagnostic and goes to logs.  `public_reason` is a fixed, caller-safe summary
and is the only text written to `leads.error_message` and returned by the API.
The service's catch-all stores `"Unexpected error: <ExceptionType>"`, never the
exception message.

**Rationale**: Playwright errors name the exact internal address and network
failure (`net::ERR_CONNECTION_REFUSED at http://10.0.0.5:8080`), which would
turn the enrichment endpoint into a port-scanning oracle even with the SSRF
guard in place.  Provider errors can echo endpoints and key fragments, and
arbitrary exception text can carry DSNs and file paths.  A fixed vocabulary of
public reasons removes that class of leak at the source rather than filtering
strings after the fact.  This also satisfies invariant 6: nothing is silenced,
because the full text is logged (`logger.error` / `logger.exception`) before
the sanitised one is stored.

**Tradeoffs**: Operators must read server logs to see why a lead failed; the
API reports only the category.  The health endpoint follows the same rule: a
failing probe is logged and reported as `503`, not surfaced as a `500`.

### D-11: `uv` for environments, locking and CI

**Decision**: Manage dependencies with `uv` (`pyproject.toml` + `uv.lock`) and
run every command through `uv run`.  `requirements.txt` is retired.  The
project sets `[tool.uv] package = false` (an application, not a library) and
declares dev tools in `[dependency-groups] dev` (`httpx`, `pytest`,
`pytest-asyncio`, `ruff`).

**Rationale**: One lockfile gives CI, Docker and a laptop the same dependency
set.  It matters here because the Playwright pip package decides which Chromium
revision is expected, so a drifting version breaks the browser, not just a
type hint.  The Dockerfile reuses the lock: a builder stage runs
`uv sync --frozen --no-dev` against copied manifests (cached until
dependencies change); the runtime stage copies the venv, installs Chromium
with `playwright install --with-deps` from that same venv, and drops to a
non-root user (uid 1000).  Playwright's own base images were rejected because
their bundled browser tracks a fixed Playwright release.  CI uses
`astral-sh/setup-uv`, `uv sync --dev`, `uv run ruff check src/` and
`uv run pytest tests/ -v`.

**Tradeoffs**: Contributors need `uv` installed.  The Dockerfile pulls
`ghcr.io/astral-sh/uv:latest`, and Compose uses `n8nio/n8n:latest`; neither is
pinned, which trades reproducibility for convenience and is listed in §9.

### D-12: Transport-agnostic `EnrichmentService` and a 202 async API

**Decision**: Move the pipeline out of `src/main.py` into
`src/services/enrichment.py` and expose it as two steps, `submit` and
`process_lead`.  The REST API answers `202 Accepted` after `submit` and runs
`process_lead` as a FastAPI background task; the CLI awaits both through
`enrich_url`.

**Rationale**: A scrape plus an LLM call takes tens of seconds, far beyond a
reasonable HTTP request, so the API must return before the work finishes.
Splitting at the point where the lead row exists makes that natural: the
`PENDING` row is the handle the client polls (`GET /api/v1/leads/{lead_id}`).
Keeping one service means the CLI and the API cannot diverge on idempotency or
failure handling.  The contract:

- **Status codes.**  `202` when work was queued, `200` when the URL is already
  `PROCESSED` and `force` is false (nothing to do), `422` for an unsafe URL.
  `submit` runs before the semaphore, so a no-op consumes no slot.
- **`reset_to_pending`.**  `insert_pending_lead` deliberately leaves an
  existing row alone (D-03), so a `FAILED` or forced lead would keep reporting
  its previous outcome while the re-run is in flight, which misleads pollers.
  Re-submission therefore resets the status and clears `error_message`
  (keeping old `enriched_data` until overwritten).
- **In-flight guard.**  `process_lead` ignores a lead ID already being
  processed in this process, so a repeated `force` request cannot start a
  second concurrent pipeline for one URL.
- **Never raise from the background.**  Failures are logged and stored as
  `FAILED` (D-10); an escaped exception would leave the lead `PENDING` forever
  because nobody awaits the task.
- **Dependency injection.**  `create_app` and `EnrichmentService` take the LLM
  client and scraper as parameters, so the API test suite runs with fakes and
  no browser, provider key or network.

**Tradeoffs and honest limits**: `BackgroundTasks` run in the API process.  A
restart mid-job leaves the lead `PENDING`; nothing sweeps stale `PENDING` rows,
and the recovery path is re-submitting the URL.  `_in_flight` and the
semaphore are per-process, so several API workers multiply effective
concurrency and the guard no longer holds across them.  A durable queue (for
example a worker with a persistent job table) would remove both limits and is
the intended next step if the API moves beyond a single instance.  The API has
no authentication of its own: Compose binds it to loopback and expects an
upstream gateway or reverse proxy to provide access control.

---

## 8. `tests/` — Offline Evaluation Suite

### Module Map

| Path | Responsibility |
|---|---|
| `pytest.ini` | `pythonpath = .` and `testpaths = tests`.  Bare `pytest` imports `src` without an editable install. |
| `conftest.py` | Autouse fixture that makes `socket.create_connection`, `socket.socket.connect` and `socket.getaddrinfo` raise `AssertionError`; the suite cannot reach a network or resolve DNS. |
| `fixtures/eval/*.md` | Scraped-page stand-ins.  The set of `*.md` names must equal the registered `_EVAL_CASES`. |

Run with `uv run pytest`.  The suite needs no provider key and no Chromium
launch; the API tests inject a fake LLM, a fake scraper and a fake host
resolver.

### What the 147 tests cover

| File | Tests | Contract under test |
|---|---|---|
| `test_api.py` | 42 | `/health` (200 and 503); `POST /enrich` (202, 200 for already-processed, `force`, 422 for unsafe, private-resolving and unresolvable hosts); failure recording without driver or vendor detail; unexpected-error handling; `GET /leads/{id}` (200 and 404); list pagination, filters and query validation; URL-syntax and host-resolution helpers. |
| `test_browser.py` | 39 | Request filter (assets aborted, private destinations blocked, inline schemes passed, per-origin verdict cache, fail-closed guard); CDP redirect guard (target extraction, private redirect failed before it is followed, public resumed, fail-closed on internal error, closed-target tolerance, response-stage enablement); `new_page` guard attachment; teardown ordering under launch failure, body exceptions, close failure and cancellation; sanitised navigation errors. |
| `test_llm_client.py` | 38 | Provider and key resolution, allowlist rejection of unsupported models, missing-key fail-fast, `api_base` restricted to OpenAI, timeout and retry forwarding and defaults, rejection of nonsensical settings, generic public errors with full logs, empty and malformed output, valid round-trip, provider loggers pinned to `WARNING`. |
| `test_scoring_regression.py` | 19 | Three golden fixtures (grounding, sub-scores, tier, DTO round-trip), eight inclusive band edges, four out-of-contract `LeadScoring` bodies, and four contract invariants (JSON-schema shape, prompt-band parity, 0 to 100 partition, fixture registration); see D-06. |
| `test_logging.py` | 4 | `configure_logging`: level applied when handlers already exist, application INFO logs emitted, unknown level fails fast. |
| `test_enrichment_service.py` | 3 | `submit`: new URL becomes `PENDING`; a `FAILED` lead is reset and its stale error cleared; a forced `PROCESSED` lead returns to `PENDING`. |
| `test_schemas.py` | 2 | One valid `EnrichedLeadPayload`; `fit_score=150` raises `ValidationError`. |
| **Total** | **147** | |

**Not covered offline**: `PostgresLeadRepository` (no sockets allowed; verified
through the Compose stack), a real browser launch, and live provider calls.
Ruff is scoped to `src/` (`ruff check src/` and `ruff format --check src/`,
both clean).  CI runs the lint check and the suite; it does not run the format
check or build the Docker image.

The decision record for why the scoring suite exists, and what it deliberately
leaves on the live path, is D-06.

---

## 9. Known Limits and Next Steps

| Area | Limit | Mitigation today | Next step |
|---|---|---|---|
| API access | No authentication. | Compose binds `app`, `postgres` and `n8n` to `127.0.0.1`; assumes an upstream gateway or reverse proxy for wider exposure. | Gateway-enforced auth, or API-key middleware. |
| Background jobs | In-process `BackgroundTasks`; a restart mid-job leaves the lead `PENDING`. | Re-submit the URL (`reset_to_pending` re-queues it). | Durable queue and a stale-`PENDING` sweeper (D-12). |
| Concurrency state | `_in_flight` and the semaphore are per-process. | Run a single API worker. | Shared queue or lock (D-12). |
| SSRF | DNS rebinding between our lookup and Chromium's; popups and cross-site iframes lack the redirect guard. | Defence in depth (D-09); loopback binding. | Egress filtering on the scraper host. |
| LLM routing | Retries only; no fallback model or provider. | `LLM_MAX_RETRIES`, `LLM_TIMEOUT_S` (D-07); failed leads can be re-submitted. | `litellm.Router` with an ordered fallback list. |
| Postgres | No offline tests; no migration tool. | Compose stack run; idempotent `CREATE ... IF NOT EXISTS`. | Containerised integration test; Alembic or similar. |
| Supply chain | `uv:latest` and `n8n:latest` images are unpinned. | `uv.lock` pins Python dependencies. | Pin image digests. |
| CI | No format check, no image build. | Format verified locally. | Add `ruff format --check src/` and a `docker build` job. |
