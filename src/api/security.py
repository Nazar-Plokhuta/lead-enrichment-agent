"""API-facing alias of the SSRF defences; the logic lives in ``src.core.url_safety``.

It sits in ``core`` because the scraper enforces the same policy on every
browser request and may not import from the API layer.
"""

from src.core.url_safety import (
    HostResolver,
    UnsafeUrlError,
    assert_public_destination,
    assert_public_host,
    resolve_host_addresses,
    validate_url_syntax,
)

__all__ = [
    "HostResolver",
    "UnsafeUrlError",
    "assert_public_destination",
    "assert_public_host",
    "resolve_host_addresses",
    "validate_url_syntax",
]
