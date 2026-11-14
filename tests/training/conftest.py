"""Training preparation tests are offline and use no installed model runtime."""
from __future__ import annotations

import socket
from urllib import request

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def reject(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Training preparation tests must not use the network")

    monkeypatch.setattr(request, "urlopen", reject)
    monkeypatch.setattr(socket, "create_connection", reject)
    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)
    yield
    assert not attempts, "A network call was attempted even if its error was handled"
