"""Tests for vault-server CLI behavior (browser auto-open)."""

import socket
import time

import pytest

from talkpipe_vault.apps import query, vault_server


@pytest.mark.parametrize(
    "host,expected",
    [
        ("0.0.0.0", "http://127.0.0.1:8002/"),
        ("", "http://127.0.0.1:8002/"),
        ("::", "http://127.0.0.1:8002/"),
        ("127.0.0.1", "http://127.0.0.1:8002/"),
        ("example.com", "http://example.com:8002/"),
    ],
)
def test_browser_url_normalizes_wildcard_hosts(host, expected):
    assert query._browser_url(host, 8002) == expected


def _capture_run_app(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        query, "run_app", lambda *a, **k: calls.update(args=a, kwargs=k)
    )
    return calls


def test_main_opens_browser_by_default(monkeypatch):
    calls = _capture_run_app(monkeypatch)
    monkeypatch.setattr("sys.argv", ["vault-server"])

    query.main()

    assert calls["kwargs"]["open_browser"] is True


def test_main_no_browser_flag_disables_open(monkeypatch):
    calls = _capture_run_app(monkeypatch)
    monkeypatch.setattr("sys.argv", ["vault-server", "--no-browser"])

    query.main()

    assert calls["kwargs"]["open_browser"] is False


def test_vault_server_main_opens_browser_by_default(monkeypatch):
    calls = {}
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    monkeypatch.setattr("sys.argv", ["vault-server"])

    vault_server.main()

    assert calls["open_browser"] is True


def test_vault_server_main_no_browser_flag_disables_open(monkeypatch):
    calls = {}
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    monkeypatch.setattr("sys.argv", ["vault-server", "--no-browser"])

    vault_server.main()

    assert calls["open_browser"] is False


def test_launch_browser_opens_once_server_accepts(monkeypatch):
    opened = []
    monkeypatch.setattr(query.webbrowser, "open", lambda url: opened.append(url))

    # A listening socket stands in for the running server.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        query._launch_browser_when_ready("127.0.0.1", port, timeout=3.0)
        deadline = time.monotonic() + 3.0
        while not opened and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        server.close()

    assert opened == [f"http://127.0.0.1:{port}/"]


def test_launch_browser_does_not_open_when_server_never_starts(monkeypatch):
    opened = []
    monkeypatch.setattr(query.webbrowser, "open", lambda url: opened.append(url))

    # Reserve a port, then close it so nothing is listening there.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    query._launch_browser_when_ready("127.0.0.1", port, timeout=0.4)
    time.sleep(0.8)

    assert opened == []
