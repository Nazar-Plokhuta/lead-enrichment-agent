# CLAUDE.md — Lead Enrichment Agent Development Guide

## Project Overview
High-performance, fully asynchronous pipeline crawling target domains, extracting normalized Markdown, and computing deterministic ICP fit scores via LLM Structured Outputs into SQLite/PostgreSQL storage. Features an engineering-focused Operator Console built with Next.js (App Router), TypeScript, Tailwind CSS, and shadcn/ui.

## Build, Test & Lint Commands

### Backend (`src/`, Python)
Dependencies and virtual environment managed by `uv` (`pyproject.toml` + `uv.lock`). Run via `uv run`:
- Run test suite: `uv run pytest`
- Run specific test file: `uv run pytest tests/test_scoring_regression.py -v`
- Lint checks: `uv run ruff check src/`
- Format checks: `uv run ruff format --check src/`
- Format code: `uv run ruff format src/`
- Run CLI manually: `uv run python -m src.main https://linear.app`
- Run API server: `uv run uvicorn src.api.app:app --host 127.0.0.1 --port 8000`

IMPORTANT: Backend test suite runs strictly offline. Outbound sockets are blocked via `socket.create_connection` autouse fixture. Never add real network calls to `tests/`.

### Frontend (`frontend/`, TypeScript/Next.js)
Dependencies managed strictly by `pnpm` (via `corepack`). Run inside `frontend/`:
- Install dependencies: `pnpm install`
- Run dev server: `pnpm dev` (runs on http://127.0.0.1:3000)
- Type check: `pnpm tsc --noEmit`
- Run unit tests: `pnpm test` (Vitest)
- Build production bundle: `pnpm build`

## Architectural Invariants (Non-Negotiable)

### Backend
1. **AsyncIO First**: All network, browser, and database I/O must use `async`/`await`. Never use blocking primitives (`time.sleep`, `requests`, `urllib`).
2. **Decoupled Architecture**: Scraper (`src/scraper/`) must never import storage or LLM modules. LLM engine (`src/llm/`) must never interact with the database or make network calls to scraped targets.
3. **Strict Schema Contracts & Zero Raw JSON Parsing**: Data between LLM and storage must strictly validate through Pydantic v2 DTOs (`EnrichedLeadPayload`). Never use `json.loads()` on unstructured LLM output strings.
4. **Defensive Scraping (D-01)**: Always use `wait_until="domcontentloaded"` combined with Playwright route interception blocking image, media, font, and stylesheet assets. Never use `networkidle`.
5. **Idempotency & Concurrency (D-03)**: Concurrency bounded by `asyncio.Semaphore`. Safe concurrency on database insertions via `INSERT OR IGNORE` + `SELECT id` (SQLite) or `ON CONFLICT DO NOTHING RETURNING` (Postgres).
6. **No Silent Failures**: Never write bare `except:`, `except Exception: pass`, or suppress exceptions without structured logging.
7. **Public Error Sanitization (D-10)**: Errors persisted to DB and returned over HTTP must strictly use sanitized `public_reason`. Internal paths, raw vendor traces, and DSNs stay in server logs only.

### Frontend
1. **Backend Unchanged**: Never modify `src/` or backend behavior for frontend needs. The frontend handles UTC date normalization, both 422 error shapes, and `status` vs `enriched_data` re-run divergence.
2. **Allowlisted Route Handler Proxy**: Never configure CORS in FastAPI. Browser requests flow through Next.js Route Handler (`app/api/[...path]/route.ts`) forwarding only allowlisted paths (`/health`, `/openapi.json`, `/api/v1/enrich`, `/api/v1/leads`, `/api/v1/leads/[id]`) to `API_ORIGIN`. Returns clean `502 Bad Gateway` on backend failure.
3. **No Direct `dangerouslySetInnerHTML`**: All LLM strings (`company_name`, `scoring_rationale`, `icebreaker`) originate from scraped web content. Render strictly as plain text (`whitespace-pre-wrap`).
4. **Docked + Sheet Dual Layout**: Wide screens (≥1280px) use a docked right pane (520–560px) to keep polling and table visible. Viewports <1280px fall back to a modal Sheet.
5. **Global Activity Query**: Poll in-flight tasks via `GET /leads?status=PENDING&page_size=100` (every 3s when active, 15s when idle). Main table list only refetches when the pending set changes or upon direct mutations.

## Frontend Design & Visual Semantics
- **Aesthetic**: Minimalist technical operator console (Linear / Vercel style).
- **Surfaces**: Strict dark mode. Background `bg-zinc-950`, cards/panels `bg-zinc-900/40 border border-zinc-800`, hover `bg-zinc-900/60`, focus ring `ring-2 ring-indigo-400`.
- **Typography**: Sans-serif (`Geist` or `Inter`) for UI prose; `JetBrains Mono` for IDs, URLs, scores, timestamps, and rubric formulas. Use `tabular-nums` for all numeric alignments.
- **Color Discipline (Tiers vs States)**:
  - **Tiers (Ordinal scale, Indigo fill weights)**:
    - `Tier 1 (High)`: `bg-indigo-500/20 text-indigo-200 ring-1 ring-inset ring-indigo-400/50`
    - `Tier 2 (Medium)`: `bg-indigo-500/10 text-indigo-300 ring-1 ring-inset ring-indigo-400/30`
    - `Tier 3 (Low)`: `bg-transparent text-zinc-300 ring-1 ring-inset ring-zinc-700`
    - `Disqualified`: `bg-transparent text-zinc-400 ring-1 ring-inset ring-zinc-800`
  - **Pipeline States (Exception-only color)**:
    - `PROCESSED`: Neutral `text-zinc-400` with `Check` icon.
    - `PENDING`: `text-sky-300 bg-sky-500/10 ring-1 ring-sky-400/30` with `Loader2` (animate-spin).
    - `FAILED`: `text-red-300 bg-red-500/10 ring-1 ring-red-400/30` with `AlertCircle`.
    - `STALLED` (>5 min in PENDING): `text-amber-300 bg-amber-500/10 ring-1 ring-amber-400/30` with `Hourglass`.
  - **Health Dot**: `bg-emerald-400` (healthy), `bg-red-400` (degraded/503), `bg-zinc-500 animate-pulse` (checking). Emerald is reserved strictly for system health.

## Code Style & Standards
- Python Version: 3.11+
- Node/TypeScript: Node 20+, TypeScript strict mode, React 19 / Next.js 15+ App Router.
- Types: 100% strict type hint coverage across Python and TypeScript.
- DTOs: Pydantic v2 (Python) and Zod/TypeScript interfaces (Frontend).
- Naming & Comments: Code, docstrings, variable names, and error messages strictly in English. Document architectural rationale ("why"), not basic mechanics ("what").

## Git & Version Control Policy
- **NO DIRECT COMMITS OR PUSHES**: Never run `git commit`, `git push`, or modifying git commands autonomously.
- The human developer handles all Git staging, commits, and pushes manually in the host terminal[cite: 16].
- When an implementation step is complete and verified, suggest a concise commit message following Conventional