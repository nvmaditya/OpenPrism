"""The autouse `_loopback_only` guard: tests can never reach the network."""

from __future__ import annotations

import socket
import threading

import pytest


def test_socket_guard_refuses_non_loopback_and_allows_loopback() -> None:
    with pytest.raises(ConnectionRefusedError, match="192.0.2.1"):
        socket.create_connection(("192.0.2.1", 80), timeout=1)
    with pytest.raises(ConnectionRefusedError, match="api.anthropic.com"):
        socket.getaddrinfo("api.anthropic.com", 443)

    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        accepted: list[socket.socket] = []
        thread = threading.Thread(target=lambda: accepted.append(server.accept()[0]))
        thread.start()
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            thread.join(5)
        assert accepted
        accepted[0].close()


def test_socket_guard_allows_localhost_by_name() -> None:
    assert socket.getaddrinfo("localhost", 80)
