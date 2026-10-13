# CLAUDE.md — Lead Enrichment Agent Development Guide

## Project Overview
High-performance, fully asynchronous pipeline crawling target domains, extracting normalized Markdown, and computing deterministic ICP fit scores via LLM Structured Outputs into SQLite/PostgreSQL storage.

## Build, Test & Lint Commands
Dependencies and the virtual environment are managed by `uv` (`pyproject.toml` + `uv.lock`). Run all commands through `uv run`:

- Run test suite: `uv run pytest`
- Run specific test file: `uv run pytest tests/test_scoring_regression.py -v`
- Lint checks: `uv run ruff check src/`
- Format checks: `uv run ruff format --check src/`
- Format code: `uv run ruff format src/`
- Run CLI manually: `uv run python -m src.main https://linear.app`

IMPORTANT: The test suite runs strictly offline. Outbound network sockets are blocked via `socket.create_connection` autouse fixture. Never add real network calls or OpenAI API requests into `tests/`.

## Architectural Invariants (Non-Negotiable)
1. **AsyncIO First**: All network, browser, and database I/O must use `async`/`await`. Never use blocking primitives (`time.sleep`, `requests`, `urllib`).
2. **Decoupled Architecture**: Scraper (`src/scraper/`) must never import storage or LLM modules. LLM engine (`src/llm/`) must never interact with the database or make network calls to scraped targets.
3. **Strict Schema Contracts & Zero Raw JSON Parsing**: Data between LLM and storage must strictly validate through Pydantic v2 DTOs (`EnrichedLeadPayload`). Never use `json.loads()` on unstructured LLM output strings.
4. **Defensive Scraping (D-01)**: Always use `wait_until="domcontentloaded"` combined with Playwright route interception blocking image, media, font, and stylesheet assets. Never use `networkidle`.
5. **Idempotency & Concurrency (D-03)**: Concurrency bounded by `asyncio.Semaphore`. Safe concurrency on database insertions via `INSERT OR IGNORE` + `SELECT id`.
6. **No Silent Failures**: Never write bare `except:`, `except Exception: pass`, or suppress exceptions without structured logging.

## Code Style & Standards
- Python Version: 3.11+
- Types: 100% strict type hint coverage across all function/method signatures.
- DTOs: Pydantic v2 exclusively (`model_validate`, `model_dump_json`).
- Naming & Comments: Code, docstrings, variable names, and error messages strictly in English. No redundant comments; document architectural rationale ("why"), not basic mechanics ("what").

## Git & Version Control Policy
- **NO DIRECT COMMITS OR PUSHES**: Never run `git commit`, `git push`, or modifying git commands autonomously.
- The human developer handles all Git staging, commits, and pushes manually in the host terminal.
- When an implementation step is complete and verified, suggest a concise commit message following Conventional Commits (e.g., `feat(storage): abstract repository interface and implement async postgres driver`), but do not execute it.