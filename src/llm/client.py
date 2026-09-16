"""
OpenAI structured-output client for lead enrichment.

Design intent
-------------
``LLMClient`` is the single integration point between the application and the
OpenAI API.  It is deliberately thin:

* It owns the async ``openai.AsyncOpenAI`` instance lifecycle.
* It composes prompts (via ``prompts`` module) and dispatches the structured-
  output call, delegating schema enforcement entirely to Pydantic + the OpenAI
  SDK's ``parse`` method.
* It translates all OpenAI SDK and Pydantic errors into the domain-specific
  ``LLMExtractionError`` so that callers never need to import ``openai``.

The client is intentionally decoupled from the storage layer — it receives a
URL and Markdown text, and returns a validated ``EnrichedLeadPayload``.  It
has no knowledge of database connections or scraper internals.
"""

from __future__ import annotations

import logging

import openai
from openai import AsyncOpenAI

from src.config import Settings, get_settings
from src.core.exceptions import LLMExtractionError
from src.llm.prompts import SYSTEM_PROMPT_TEMPLATE, build_user_prompt
from src.llm.schemas import EnrichedLeadPayload

logger = logging.getLogger(__name__)

# Temperature close to zero produces near-deterministic scoring runs, which is
# critical for reproducibility in a lead-qualification context.  A non-zero
# value (0.1) leaves a small window for natural language variety in rationale
# and outreach copy without destabilising numeric scores.
_TEMPERATURE: float = 0.1


class LLMClient:
    """Async wrapper around OpenAI's Structured Outputs endpoint.

    Instantiate once per process (or per worker in a concurrent pipeline) and
    reuse across multiple ``enrich_lead`` calls — the underlying
    ``AsyncOpenAI`` client manages connection pooling internally.

    Args:
        settings: Application settings instance.  Defaults to the process-wide
                  singleton returned by ``get_settings()``.  Pass an explicit
                  instance in tests to avoid touching the real environment.

    Example::

        client = LLMClient()
        payload = await client.enrich_lead(url, markdown_text)
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings: Settings = settings or get_settings()
        self._client: AsyncOpenAI = AsyncOpenAI(
            api_key=self._settings.OPENAI_API_KEY.get_secret_value(),
            # Forward a custom base URL only when explicitly configured —
            # omitting the kwarg entirely lets the SDK default to the
            # official OpenAI endpoint without any None-handling quirks.
            **({"base_url": self._settings.OPENAI_BASE_URL} if self._settings.OPENAI_BASE_URL else {}),
        )

    async def enrich_lead(self, url: str, markdown_content: str) -> EnrichedLeadPayload:
        """Enrich a single lead by analysing scraped page content via the LLM.

        Uses OpenAI's native structured-output path
        (``client.beta.chat.completions.parse``) so the SDK validates the
        model response against ``EnrichedLeadPayload`` before returning.
        No manual JSON parsing or dict-to-model coercion is performed.

        Args:
            url:              The canonical URL of the company page that was
                              scraped.  Included in the user prompt as a
                              reference anchor and in any raised errors.
            markdown_content: Normalised Markdown text produced by the
                              ``cleaner`` module, token-budgeted to ≤ 4,000
                              tokens.

        Returns:
            A fully validated ``EnrichedLeadPayload`` instance.

        Raises:
            LLMExtractionError: On any OpenAI API error (network, auth, rate
                limit, content filter) or if the structured-output response
                fails Pydantic validation.  The ``url`` and ``reason``
                attributes carry the full context for upstream logging.
        """
        user_prompt = build_user_prompt(url, markdown_content)

        logger.debug("Dispatching structured-output request for '%s'", url)

        try:
            completion = await self._client.beta.chat.completions.parse(
                model=self._settings.OPENAI_MODEL,
                temperature=_TEMPERATURE,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT_TEMPLATE},
                    {"role": "user", "content": user_prompt},
                ],
                response_format=EnrichedLeadPayload,
            )
        except openai.APIError as exc:
            # Covers authentication errors, rate limits, network failures,
            # content-filter refusals, and any other OpenAI API-level error.
            raise LLMExtractionError(
                url=url,
                reason=f"OpenAI API error ({type(exc).__name__}): {exc}",
            ) from exc

        parsed = completion.choices[0].message.parsed

        if parsed is None:
            # The SDK sets ``parsed`` to None when the model triggers a
            # content-filter refusal or returns a non-parseable response.
            refusal = completion.choices[0].message.refusal or "no parsed payload returned"
            raise LLMExtractionError(url=url, reason=f"Structured output refused: {refusal}")

        logger.info(
            "Lead enriched | url='%s' company='%s' score=%d tier='%s'",
            url,
            parsed.company_name,
            parsed.scoring.fit_score,
            parsed.scoring.fit_tier,
        )

        return parsed
