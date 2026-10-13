"""
Domain-specific exception hierarchy for the lead-enrichment agent.

Design intent
-------------
All exceptions inherit from `LeadEnrichmentError` so callers can catch the
entire family with a single `except LeadEnrichmentError` when broad recovery is
appropriate, while still allowing targeted handling via the sub-classes.

Keeping exceptions in `core` enforces the architectural invariant that every
module (scraper, llm, storage) can raise typed errors without creating circular
imports — none of them need to import each other to signal failures.
"""


class LeadEnrichmentError(Exception):
    """Base class for all project-specific errors.

    Subclasses carry two descriptions of a failure.  ``reason`` is the full
    diagnostic for server-side logs and may contain driver or vendor detail
    (internal hostnames, masked credentials).  ``public_reason`` is the fixed,
    caller-safe summary that is persisted on the lead and served by the API.
    """

    public_reason: str = "Enrichment failed"


class ScrapeError(LeadEnrichmentError):
    """Raised when the scraper cannot successfully retrieve page content.

    Wraps lower-level Playwright exceptions (timeouts, navigation failures,
    connection resets) so that callers in the orchestration layer are insulated
    from the Playwright API surface.

    Attributes:
        url: The URL that triggered the failure.
        reason: Diagnostic description of the failure mode, for logs only.
        public_reason: Sanitised summary safe to store and return to API
            callers.  Raw browser messages must never be passed here: they name
            the exact internal address and error, which would turn the API into
            a port-scanning oracle.
    """

    def __init__(
        self,
        url: str,
        reason: str,
        *,
        public_reason: str = "Page could not be retrieved",
    ) -> None:
        self.url = url
        self.reason = reason
        self.public_reason = public_reason
        super().__init__(f"Scrape failed for '{url}': {reason}")


class LLMExtractionError(LeadEnrichmentError):
    """Raised when the LLM client cannot produce a validated structured output.

    Mirrors the structure of ``ScrapeError`` so that the orchestration layer
    can handle both failure modes uniformly (log url + reason, mark lead FAILED).

    Attributes:
        url:    The company URL that was being enriched when the error occurred.
        reason: Diagnostic description of the failure mode (API error, schema
                validation failure, content-filter refusal, etc.), for logs only.
        public_reason: Sanitised summary safe to store and return to API
                callers; provider error text can echo endpoints and key
                fragments and must not reach it.
    """

    def __init__(
        self, url: str, reason: str, *, public_reason: str = "LLM enrichment failed"
    ) -> None:
        self.url = url
        self.reason = reason
        self.public_reason = public_reason
        super().__init__(f"LLM extraction failed for '{url}': {reason}")


class StorageError(LeadEnrichmentError):
    """Raised when a persistence operation fails in an unrecoverable way."""
