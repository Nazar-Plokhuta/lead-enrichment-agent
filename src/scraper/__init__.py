"""
Scraper package: headless browser lifecycle, page fetching, and HTML normalisation.

Public surface
--------------
- `browser.BrowserManager`: async context manager for Playwright Chromium.
- `fetcher.fetch_page_markdown`: end-to-end URL → clean Markdown coroutine.
- `cleaner.extract_markdown`: trafilatura-powered HTML → Markdown transformer.

Architectural invariant: this package never imports `storage` or `llm` modules.
"""
