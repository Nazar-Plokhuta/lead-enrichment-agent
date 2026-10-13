"""Offline tests for provider routing, request parameters and error mapping."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from pydantic import ValidationError

from src.config import Settings
from src.core.exceptions import LLMExtractionError
from src.llm import client as client_module
from src.llm.client import LLMClient, _resolve_api_key
from src.llm.schemas import (
    CompanyAnalysis,
    EnrichedLeadPayload,
    LeadScoring,
    OutreachStrategy,
)

pytestmark = pytest.mark.asyncio

_URL = "https://linear.app"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "OPENAI_API_KEY": "sk-openai",
        "ANTHROPIC_API_KEY": "sk-ant",
        "OPENROUTER_API_KEY": "sk-or",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _valid_json() -> str:
    return EnrichedLeadPayload(
        company_name="Linear",
        analysis=CompanyAnalysis(
            industry="B2B SaaS",
            target_audience="Engineering teams",
            value_proposition="Issue tracking.",
            pain_points=["Slow tools"],
        ),
        scoring=LeadScoring(
            fit_tier="Tier 1 (High)", fit_score=90, scoring_rationale="Strong fit."
        ),
        outreach=OutreachStrategy(icebreaker="Hi.", suggested_angle="Speed."),
    ).model_dump_json()


def _response(content: str | None) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class FakeCompletion:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.kwargs: dict[str, Any] = {}
        self._result = result if result is not None else _response(_valid_json())
        self._error = error

    async def __call__(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._result


def _patch(monkeypatch: pytest.MonkeyPatch, fake: FakeCompletion) -> None:
    monkeypatch.setattr(client_module.litellm, "acompletion", fake)


# --- provider routing (H6) -------------------------------------------------


@pytest.mark.parametrize(
    ("model", "key"),
    [
        ("gpt-4o-mini", "sk-openai"),
        ("o3-mini", "sk-openai"),
        ("openai/gpt-4o", "sk-openai"),
        ("claude-3-5-sonnet-latest", "sk-ant"),
        ("anthropic/claude-3-5-sonnet-latest", "sk-ant"),
        ("openrouter/anthropic/claude-3.5-sonnet", "sk-or"),
        ("OpenRouter/openai/gpt-4o", "sk-or"),
    ],
)
async def test_model_selects_matching_key(model: str, key: str) -> None:
    assert _resolve_api_key(_settings(LLM_MODEL=model)).get_secret_value() == key


@pytest.mark.parametrize(
    "model",
    [
        "gemini/gemini-1.5-pro",
        "azure/my-gpt4-deployment",
        "bedrock/anthropic.claude-3-sonnet",
        "vertex_ai/claude-3-5-sonnet",
        "groq/llama3-70b",
        "mistral-large-latest",
        "gemini-pro",
        "",
    ],
)
async def test_unsupported_model_is_rejected_instead_of_using_the_openai_key(
    model: str,
) -> None:
    with pytest.raises(ValueError, match="Unsupported LLM_MODEL"):
        LLMClient(_settings(LLM_MODEL=model))


@pytest.mark.parametrize(
    ("model", "missing"),
    [
        ("gpt-4o-mini", "OPENAI_API_KEY"),
        ("claude-3-5-sonnet-latest", "ANTHROPIC_API_KEY"),
        ("openrouter/x/y", "OPENROUTER_API_KEY"),
    ],
)
async def test_missing_key_for_the_selected_provider_fails_fast(
    model: str, missing: str
) -> None:
    settings = _settings(LLM_MODEL=model, **{missing: None})
    with pytest.raises(ValueError, match=missing):
        LLMClient(settings)


@pytest.mark.parametrize(
    ("model", "expect_base"),
    [
        ("gpt-4o-mini", True),
        ("openai/my-model", True),
        ("claude-3-5-sonnet-latest", False),
        ("openrouter/anthropic/claude-3.5-sonnet", False),
    ],
)
async def test_api_base_is_only_sent_to_openai_models(
    monkeypatch: pytest.MonkeyPatch, model: str, expect_base: bool
) -> None:
    fake = FakeCompletion()
    _patch(monkeypatch, fake)
    llm = LLMClient(_settings(LLM_MODEL=model, OPENAI_BASE_URL="https://proxy.test/v1"))

    await llm.enrich_lead(_URL, "# page")

    assert ("api_base" in fake.kwargs) is expect_base
    if expect_base:
        assert fake.kwargs["api_base"] == "https://proxy.test/v1"


async def test_ignored_base_url_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=client_module.logger.name):
        LLMClient(
            _settings(
                LLM_MODEL="claude-3-5-sonnet-latest", OPENAI_BASE_URL="https://p.test"
            )
        )
    assert "OPENAI_BASE_URL is ignored" in caplog.text


# --- timeout and retries (H4) ----------------------------------------------


async def test_timeout_and_retry_policy_are_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCompletion()
    _patch(monkeypatch, fake)

    await LLMClient(_settings(LLM_TIMEOUT_S=12.5, LLM_MAX_RETRIES=4)).enrich_lead(
        _URL, "x"
    )

    assert fake.kwargs["timeout"] == 12.5
    assert fake.kwargs["num_retries"] == 4


async def test_defaults_are_a_45s_timeout_with_two_retries() -> None:
    settings = _settings()
    assert settings.LLM_TIMEOUT_S == 45.0
    assert settings.LLM_MAX_RETRIES == 2


@pytest.mark.parametrize("overrides", [{"LLM_TIMEOUT_S": 0}, {"LLM_MAX_RETRIES": -1}])
async def test_nonsensical_timeout_settings_are_rejected(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        _settings(**overrides)


# --- error mapping and sanitisation (H7) -----------------------------------


async def test_provider_error_is_logged_in_full_but_exposed_generically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_text = "Incorrect API key provided: sk-proj-****abcd"
    error = openai.APIConnectionError(
        message=secret_text, request=httpx.Request("POST", "https://api.openai.com/v1")
    )
    _patch(monkeypatch, FakeCompletion(error=error))

    with pytest.raises(LLMExtractionError) as info:
        await LLMClient(_settings()).enrich_lead(_URL, "x")

    assert "sk-proj" in info.value.reason
    assert (
        info.value.public_reason == "LLM provider request failed (APIConnectionError)"
    )
    assert "sk-proj" not in info.value.public_reason


async def test_empty_response_has_a_fixed_public_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, FakeCompletion(result=_response(None)))
    with pytest.raises(LLMExtractionError) as info:
        await LLMClient(_settings()).enrich_lead(_URL, "x")
    assert info.value.public_reason == "LLM returned an empty response"


@pytest.mark.parametrize(
    "content", ["not json at all", '{"company_name": "Acme"}', '{"fit_score": 999}']
)
async def test_malformed_or_partial_output_is_rejected_by_the_schema(
    monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    _patch(monkeypatch, FakeCompletion(result=_response(content)))
    with pytest.raises(LLMExtractionError) as info:
        await LLMClient(_settings()).enrich_lead(_URL, "x")
    assert info.value.public_reason == "LLM response did not match the expected schema"


async def test_valid_output_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, FakeCompletion())
    payload = await LLMClient(_settings()).enrich_lead(_URL, "x")
    assert payload.company_name == "Linear"
    assert payload.scoring.fit_score == 90


# --- third-party log noise (M11) -------------------------------------------


@pytest.mark.parametrize(
    "name", ["LiteLLM", "LiteLLM Router", "httpx", "httpcore", "openai"]
)
async def test_provider_loggers_are_pinned_to_warning(name: str) -> None:
    assert logging.getLogger(name).level == logging.WARNING
