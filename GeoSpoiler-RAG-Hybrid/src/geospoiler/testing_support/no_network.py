"""Opt-in network blocker for unit-test runs."""

from __future__ import annotations

import os
import socket
from contextvars import ContextVar
from typing import Any


class NetworkBlockedError(RuntimeError):
    """Raised when code tries to open a network connection in no-network mode."""


_INSTALLED = False
_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CREATE_CONNECTION = socket.create_connection
_ORIGINAL_SOCKETPAIR = socket.socketpair
_CREATING_SOCKETPAIR: ContextVar[bool] = ContextVar("test_socketpair", default=False)


def install_no_network_guard() -> None:
    """Block outbound sockets for deterministic unit tests."""
    global _INSTALLED
    if _INSTALLED:
        return

    def blocked_connect(self: socket.socket, address: Any) -> None:
        # Windows implements socketpair with a loopback connection. Permit only
        # that synchronous, context-local operation, not general localhost IO.
        if _CREATING_SOCKETPAIR.get() and isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return _ORIGINAL_CONNECT(self, address)
        raise NetworkBlockedError(f"Network access is disabled for unit tests: {address!r}")

    def blocked_create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        raise NetworkBlockedError(f"Network access is disabled for unit tests: {address!r}")

    def local_socketpair(*args: Any, **kwargs: Any) -> tuple[socket.socket, socket.socket]:
        token = _CREATING_SOCKETPAIR.set(True)
        try:
            return _ORIGINAL_SOCKETPAIR(*args, **kwargs)
        finally:
            _CREATING_SOCKETPAIR.reset(token)

    socket.socket.connect = blocked_connect
    socket.create_connection = blocked_create_connection
    socket.socketpair = local_socketpair
    _INSTALLED = True


def uninstall_no_network_guard() -> None:
    """Restore socket functions. Intended for focused helper tests only."""
    global _INSTALLED
    if not _INSTALLED:
        return
    socket.socket.connect = _ORIGINAL_CONNECT
    socket.create_connection = _ORIGINAL_CREATE_CONNECTION
    socket.socketpair = _ORIGINAL_SOCKETPAIR
    _INSTALLED = False


def install_from_env() -> None:
    if os.getenv("GEOSPOILER_NO_NETWORK", "").lower() in {"1", "true", "yes"}:
        install_no_network_guard()
