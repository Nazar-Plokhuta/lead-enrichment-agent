"""Shared fixtures; the suite must stay strictly offline."""

from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def _block_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a connection or does a DNS lookup."""

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("test attempted a network operation")

    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
