"""
Provider-agnostic structured-output client for lead enrichment.

Design intent
-------------
``LLMClient`` is the single integration point between the application and
whichever LLM provider is configured (OpenAI, Anthropic, OpenRouter, ...).  It
is deliberately thin:

* It routes every call through ``litellm.acompletion`` so switching providers
  is a configuration change (``LLM_MODEL``), not a code change.
* It composes prompts (via ``prompts`` module) and requests schema-constrained
  output by passing ``EnrichedLeadPayload`` as ``response_format``.
* It validates the response with ``EnrichedLeadPayload.model_validate_json`` —
  never ``json.loads`` — so a schema mismatch fails fast instead of leaking an
  unvalidated dict downstream.
* It translates every provider and Pydantic error into the domain-specific
  ``LLMExtractionError`` so that callers never need to import ``litellm``.

The client is intentionally decoupled from the storage layer — it receives a
URL and Markdown text, and returns a validated ``EnrichedLeadPayload``.  It
has no knowledge of database connections or scraper internals.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

# Must be set before ``litellm`` is imported: by default litellm downloads its
# model-cost map from GitHub at import time, which would open a socket and trip
# the offline test guard (and add a startup network dependency in production).
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm
import openai
from pydantic import SecretStr, ValidationError

from src.config import Settings, get_settings
from src.core.exceptions import LLMExtractionError
from src.llm.prompts import SYSTEM_PROMPT_TEMPLATE, build_user_prompt
from src.llm.schemas import EnrichedLeadPayload

# No remote telemetry, and silently discard request params a given provider
# does not support (e.g. ``temperature`` on reasoning models) instead of failing.
litellm.telemetry = False
litellm.drop_params = True

# litellm and the HTTP stack beneath it log request details at DEBUG (and every
# call at INFO for httpx).  Pinning them here keeps ``LOG_LEVEL=DEBUG`` for the
# application from also dumping provider traffic, headers included.
for _noisy_logger in (
    "LiteLLM",
    "LiteLLM Router",
    "LiteLLM Proxy",
    "httpx",
    "httpcore",
    "openai",
):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Temperature close to zero produces near-deterministic scoring runs, which is
# critical for reproducibility in a lead-qualification context.  A non-zero
# value (0.1) leaves a small window for natural language variety in rationale
# and outreach copy without destabilising numeric scores.
_TEMPERATURE: float = 0.1

Provider = Literal["openai", "anthropic", "openrouter"]

# Explicit ``<provider>/<model>`` prefixes this client can authenticate.
_PREFIXED_PROVIDERS: dict[str, Provider] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "openrouter": "openrouter",
}
# Unprefixed names litellm resolves to a provider by convention.
_OPENAI_BARE_PREFIXES = ("gpt-", "chatgpt-", "ft:gpt-", "o1", "o3", "o4")
_ANTHROPIC_BARE_PREFIX = "claude"

_API_KEY_NAMES: dict[Provider, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def _resolve_provider(model: str) -> Provider:
    """Map ``LLM_MODEL`` to one of the providers whose credentials we hold.

    Anything else is rejected rather than defaulted to OpenAI: litellm would
    happily route ``gemini/...`` or ``azure/...`` to that vendor, and the only
    key we would have to send is the OpenAI one.

    Raises:
        ValueError: The model does not belong to a supported provider.
    """
    name = model.strip().lower()
    if "/" in name:
        provider = _PREFIXED_PROVIDERS.get(name.split("/", 1)[0])
        if provider is not None:
            return provider
    elif name.startswith(_ANTHROPIC_BARE_PREFIX):
        return "anthropic"
    elif name.startswith(_OPENAI_BARE_PREFIXES):
        return "openai"

    raise ValueError(
        f"Unsupported LLM_MODEL '{model}': use a gpt-*/o-series name or 'openai/<model>' "
        "(OPENAI_API_KEY), a claude-* name or 'anthropic/<model>' (ANTHROPIC_API_KEY), "
        "or 'openrouter/<vendor>/<model>' (OPENROUTER_API_KEY)"
    )


def _resolve_api_key(settings: Settings) -> SecretStr:
    """Pick the credential matching the provider implied by ``LLM_MODEL``.

    Keys are passed to litellm explicitly because ``.env`` values loaded by
    pydantic-settings are not exported to ``os.environ``, which is where
    litellm would otherwise look for them.

    Raises:
        ValueError: The model's provider is unsupported, or the key it needs
            is not set.  Failing at construction beats an error per lead.
    """
    provider = _resolve_provider(settings.LLM_MODEL)
    name = _API_KEY_NAMES[provider]
    key: SecretStr | None = getattr(settings, name)

    if key is None:
        raise ValueError(f"LLM_MODEL '{settings.LLM_MODEL}' requires {name} to be set")
    return key


class LLMClient:
    """Async, provider-agnostic wrapper around structured-output completions.

    Instantiate once per process (or per worker in a concurrent pipeline) and
    reuse across multiple ``enrich_lead`` calls.

    Args:
        settings: Application settings instance.  Defaults to the process-wide
                  singleton returned by ``get_settings()``.  Pass an explicit
                  instance in tests to avoid touching the real environment.

    Raises:
        ValueError: The API key required by ``settings.LLM_MODEL`` is missing.

    Example::

        client = LLMClient()
        payload = await client.enrich_lead(url, markdown_text)
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings: Settings = settings or get_settings()
        self._api_key: SecretStr = _resolve_api_key(self._settings)

        # ``OPENAI_BASE_URL`` is an OpenAI-compatible endpoint override.  Sent
        # alongside another provider's key it would hand that key to the proxy.
        self._api_base: str | None = None
        if self._settings.OPENAI_BASE_URL:
            if _resolve_provider(self._settings.LLM_MODEL) == "openai":
                self._api_base = self._settings.OPENAI_BASE_URL
            else:
                logger.warning(
                    "OPENAI_BASE_URL is ignored: LLM_MODEL '%s' is not an OpenAI model",
                    self._settings.LLM_MODEL,
                )

    async def enrich_lead(self, url: str, markdown_content: str) -> EnrichedLeadPayload:
        """Enrich a single lead by analysing scraped page content via the LLM.

        Requests schema-constrained output with
        ``response_format=EnrichedLeadPayload`` and validates the returned JSON
        with ``EnrichedLeadPayload.model_validate_json``.  No manual JSON
        parsing or dict-to-model coercion is performed.

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
            LLMExtractionError: On any provider error (network, auth, rate
                limit, context length, content filter), an empty response, or
                a response that fails Pydantic validation.  The ``url`` and
                ``reason`` attributes carry the full context for upstream
                logging.
        """
        user_prompt = build_user_prompt(url, markdown_content)

        logger.debug(
            "Dispatching structured-output request for '%s' (model=%s)",
            url,
            self._settings.LLM_MODEL,
        )

        try:
            response = await litellm.acompletion(
                model=self._settings.LLM_MODEL,
                api_key=self._api_key.get_secret_value(),
                # Forward a custom base URL only when explicitly configured so
                # litellm keeps its per-provider default endpoints otherwise.
                **({"api_base": self._api_base} if self._api_base else {}),
                timeout=self._settings.LLM_TIMEOUT_S,
                num_retries=self._settings.LLM_MAX_RETRIES,
                temperature=_TEMPERATURE,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT_TEMPLATE},
                    {"role": "user", "content": user_prompt},
                ],
                response_format=EnrichedLeadPayload,
            )
        except openai.OpenAIError as exc:
            # litellm maps every provider failure (auth, rate limit, context
            # window, timeout, connection, content policy) onto subclasses of
            # ``openai.OpenAIError``, so one clause covers all providers.
            logger.error(
                "LLM provider error for '%s' (%s): %s", url, type(exc).__name__, exc
            )
            raise LLMExtractionError(
                url=url,
                reason=f"LLM enrichment failed: {exc}",
                public_reason=f"LLM provider request failed ({type(exc).__name__})",
            ) from exc

        content = response.choices[0].message.content
        if not content:
            raise LLMExtractionError(
                url=url,
                reason="LLM enrichment failed: empty response (possible refusal)",
                public_reason="LLM returned an empty response",
            )

        try:
            parsed = EnrichedLeadPayload.model_validate_json(content)
        except ValidationError as exc:
            logger.error("Schema mismatch in LLM response for '%s': %s", url, exc)
            raise LLMExtractionError(
                url=url,
                reason=f"LLM enrichment failed: response violates EnrichedLeadPayload schema: {exc}",
                public_reason="LLM response did not match the expected schema",
            ) from exc

        logger.info(
            "Lead enriched | url='%s' company='%s' score=%d tier='%s'",
            url,
            parsed.company_name,
            parsed.scoring.fit_score,
            parsed.scoring.fit_tier,
        )

        return parsed
