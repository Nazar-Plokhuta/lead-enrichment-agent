"""
LLM extraction and structured scoring layer.

Exposes the public surface used by the orchestration layer:
- ``LLMClient``: async wrapper around OpenAI Structured Outputs.
- ``EnrichedLeadPayload`` (and its constituent models): the canonical DTO that
  flows from LLM → storage.  No other module should define or re-export these.
"""

from src.llm.client import LLMClient
from src.llm.schemas import (
    CompanyAnalysis,
    EnrichedLeadPayload,
    LeadScoring,
    OutreachStrategy,
)

__all__ = [
    "LLMClient",
    "CompanyAnalysis",
    "LeadScoring",
    "OutreachStrategy",
    "EnrichedLeadPayload",
]
