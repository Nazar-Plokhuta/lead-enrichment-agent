"""
HTML → Markdown normalisation using trafilatura.

Design intent
-------------
Raw DOM from a headless browser contains enormous amounts of noise: navigation
bars, cookie consent banners, footers, advertisement frames, inline scripts, and
tracking pixels.  Sending that directly to an LLM wastes the token budget and
degrades extraction quality by diluting the signal.

`extract_markdown` delegates boilerplate removal to trafilatura (which uses
heuristics derived from the readability algorithm) and then applies a hard
character ceiling so callers never produce an oversized payload regardless of
how verbose the source page is.

The truncation strategy cuts on a word boundary rather than a raw character
index to avoid feeding the LLM a sentence that is split mid-word.
"""

from __future__ import annotations

import logging

import trafilatura

logger = logging.getLogger(__name__)

# trafilatura extraction configuration — tuned for maximum signal retention
# while still stripping structural boilerplate.
_TRAFILATURA_CONFIG = trafilatura.settings.use_config()
_TRAFILATURA_CONFIG.set("DEFAULT", "EXTRACTION_TIMEOUT", "0")


def extract_markdown(html_content: str, max_chars: int = 15_000) -> str:
    """Strip boilerplate from raw HTML and return clean Markdown/plain text.

    The function applies trafilatura's main extraction pipeline which handles:
    - Navigation menus and sidebars
    - Cookie banners and GDPR overlays
    - Page headers and footers
    - Inline JavaScript and CSS
    - Advertisement containers

    The resulting text is then truncated at `max_chars` on the nearest word
    boundary to ensure the downstream LLM call stays within its context budget.

    Args:
        html_content: Raw HTML string as returned by Playwright's
            `page.content()`.
        max_chars: Maximum character length of the returned string.
            Defaults to 15,000 (~3,750 tokens at ~4 chars/token), leaving
            ample headroom for system prompts in an 8k-token context window.

    Returns:
        Clean Markdown/plain-text representation of the page's primary content.
        Returns an empty string if trafilatura cannot identify meaningful
        content (e.g. purely JavaScript-rendered shells with no static DOM).
    """
    extracted: str | None = trafilatura.extract(
        html_content,
        config=_TRAFILATURA_CONFIG,
        include_tables=True,
        include_links=False,      # URLs add noise without semantic value
        include_images=False,
        output_format="markdown",
        no_fallback=False,        # Allow readability fallback for thin pages
    )

    if not extracted:
        logger.warning("trafilatura returned no content — HTML may be a JavaScript shell")
        return ""

    return _truncate_on_word_boundary(extracted, max_chars)


def _truncate_on_word_boundary(text: str, max_chars: int) -> str:
    """Return `text` truncated to at most `max_chars` characters.

    Truncation always happens at the last whitespace character before the
    ceiling so that the LLM never receives a word split in the middle.  A
    trailing ellipsis signals to the model that the content was intentionally
    cut rather than implying the page ended mid-sentence.

    Args:
        text: Source string, may be longer than `max_chars`.
        max_chars: Hard ceiling on the returned string length (excluding the
            appended " …" marker when truncation occurs).

    Returns:
        The original string if it fits within `max_chars`, otherwise a
        word-boundary-truncated version with a trailing " …" appended.
    """
    if len(text) <= max_chars:
        return text

    # Walk backwards from the ceiling to find the last whitespace character.
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")

    if last_space == -1:
        # Pathological case: a single token longer than max_chars.
        return truncated + " …"

    return truncated[:last_space] + " …"
