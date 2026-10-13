"""
SSRF defence shared by the API front door and the scraper's browser.

The scraper drives a real browser at whatever URL it is given, so an untrusted
caller could otherwise aim it at loopback services, the cloud metadata endpoint
(``169.254.169.254``) or the internal network.  Validation is split in two:

* ``validate_url_syntax`` — pure and synchronous: scheme, malformed-URL tricks,
  blocked hostnames and IP literals.  Cheap enough for a Pydantic validator.
* ``assert_public_host`` — resolves non-literal hostnames and rejects any name
  that points at a non-public address (DNS records and obfuscated forms such as
  ``2130706433`` or ``0x7f.1`` only reveal their target once resolved).

``assert_public_destination`` combines both for a single browser request.  The
scraper applies it to every request, redirect hops included, which closes the
"public URL that 302s to ``169.254.169.254``" hole that a front-door check
alone leaves open.

Known limit: Chromium resolves the name again when it connects (DNS rebinding
between our lookup and its own), so this is not a substitute for egress
filtering on the scraper host.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_MAX_URL_LENGTH = 2048
_RESOLVE_TIMEOUT_S = 5.0

_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
# Reserved for local/internal use (RFC 6761, RFC 6762) or commonly used for
# cloud-internal names such as ``metadata.google.internal``.
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")

HostResolver = Callable[[str, int], Awaitable[list[str]]]


class UnsafeUrlError(ValueError):
    """Raised when a URL is malformed or targets a non-public destination."""


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # IPv4-mapped IPv6 (``::ffff:127.0.0.1``) must be judged by the embedded address.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # ``is_global`` excludes private, loopback, link-local, reserved and CGNAT
    # ranges; multicast is not covered by it for IPv4, hence the explicit check.
    return ip.is_global and not ip.is_multicast


def validate_url_syntax(url: str) -> str:
    """Validate *url* without any I/O and return it stripped.

    Raises:
        UnsafeUrlError: The URL is malformed, not http(s), or its host is a
            blocked name or a non-public IP literal.
    """
    url = url.strip()
    if not url:
        raise UnsafeUrlError("URL must not be empty")
    if len(url) > _MAX_URL_LENGTH:
        raise UnsafeUrlError(f"URL must not exceed {_MAX_URL_LENGTH} characters")
    # Browsers treat ``\`` as ``/`` while ``urlsplit`` does not, so
    # ``http://127.0.0.1\@example.com`` would pass here yet navigate to loopback.
    if "\\" in url or any(
        ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url
    ):
        raise UnsafeUrlError(
            "URL contains whitespace, control characters or backslashes"
        )

    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError on a malformed or out-of-range port
    except ValueError as exc:
        raise UnsafeUrlError(f"URL is malformed: {exc}") from exc

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError("Only http and https URLs are allowed")

    _check_hostname((parts.hostname or "").rstrip(".").lower())
    return url


def _check_hostname(host: str) -> None:
    """Reject empty, blocked-by-name and non-public IP-literal hosts (no I/O)."""
    if not host:
        raise UnsafeUrlError("URL must include a hostname")
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeUrlError(f"Host '{host}' is not allowed")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # a hostname; its resolved addresses are checked separately
    if not _is_public_ip(ip):
        raise UnsafeUrlError(f"Address '{host}' is private or reserved")


async def resolve_host_addresses(host: str, port: int) -> list[str]:
    """Resolve *host* to its IP address strings via the event loop's resolver."""
    infos = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
        timeout=_RESOLVE_TIMEOUT_S,
    )
    return [str(info[4][0]) for info in infos]


async def assert_public_host(
    url: str, resolver: HostResolver = resolve_host_addresses
) -> None:
    """Reject *url* if its hostname resolves to any non-public address.

    Every resolved address is checked, not just the first, because the browser
    may pick any of them.  IP literals were already vetted by
    ``validate_url_syntax`` and need no lookup.

    Raises:
        UnsafeUrlError: The host cannot be resolved or resolves to a
            private/reserved address.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return

    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    try:
        addresses = await resolver(host, port)
    except (OSError, TimeoutError) as exc:
        raise UnsafeUrlError(f"Host '{host}' could not be resolved") from exc
    if not addresses:
        raise UnsafeUrlError(f"Host '{host}' could not be resolved")

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop IPv6 zone id
        except ValueError as exc:
            raise UnsafeUrlError(
                f"Host '{host}' resolved to an invalid address"
            ) from exc
        if not _is_public_ip(ip):
            raise UnsafeUrlError(
                f"Host '{host}' resolves to a private or reserved address"
            )


async def assert_public_destination(
    url: str, resolver: HostResolver = resolve_host_addresses
) -> None:
    """Vet one browser request URL: scheme, host name/literal, resolved addresses.

    Unlike ``validate_url_syntax`` this does not cap the URL length or reject
    odd characters: sub-resource URLs a page requests are legitimately long and
    Chromium has already normalised them, so only the destination matters.

    Raises:
        UnsafeUrlError: The scheme is not http(s) or the host is non-public.
    """
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError as exc:
        raise UnsafeUrlError(f"URL is malformed: {exc}") from exc
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError("Only http and https URLs are allowed")
    _check_hostname((parts.hostname or "").rstrip(".").lower())
    await assert_public_host(url, resolver)
