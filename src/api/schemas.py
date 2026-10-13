"""Request and response models for the REST API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, field_validator

from src.api.security import UnsafeUrlError, validate_url_syntax

LeadStatus = Literal["PENDING", "PROCESSED", "FAILED"]
FitTier = Literal["Tier 1 (High)", "Tier 2 (Medium)", "Tier 3 (Low)", "Disqualified"]

# Stored ``enriched_data`` arrives from both drivers as JSON text; the adapter
# parses it without a hand-rolled ``json.loads``.
_JSON_OBJECT = TypeAdapter(dict[str, Any])


class EnrichRequest(BaseModel):
    """Body of ``POST /api/v1/enrich``.

    ``url`` stays a plain string (not ``HttpUrl``) so the value stored and
    returned is exactly what the caller sent; ``HttpUrl`` would rewrite
    ``https://linear.app`` to ``https://linear.app/`` and fork the identity key
    shared with the CLI.
    """

    url: str = Field(description="Public http(s) URL of the company page to enrich.")
    force: bool = Field(
        default=False,
        description="Re-enrich even if the URL has already been processed.",
    )

    @field_validator("url")
    @classmethod
    def _reject_unsafe_url(cls, value: str) -> str:
        try:
            return validate_url_syntax(value)
        except UnsafeUrlError as exc:
            raise ValueError(str(exc)) from exc


class EnrichResponse(BaseModel):
    lead_id: int
    url: str
    status: str
    message: str


class LeadResponse(BaseModel):
    id: int
    url: str
    status: LeadStatus
    company_name: str | None = None
    fit_score: int | None = None
    fit_tier: str | None = None
    enriched_data: dict[str, Any] | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("enriched_data", mode="before")
    @classmethod
    def _parse_stored_json(cls, value: object) -> object:
        if isinstance(value, str | bytes):
            return _JSON_OBJECT.validate_json(value)
        return value


class LeadListResponse(BaseModel):
    items: list[LeadResponse]
    total: int
    page: int
    page_size: int


class HealthResponse(BaseModel):
    status: str
    database: str
