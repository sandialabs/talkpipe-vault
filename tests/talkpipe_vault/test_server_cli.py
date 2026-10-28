"""Tests for vault-server CLI behavior: browser auto-open, port choice,
already-running detection, and the packaged launcher icons."""

import http.server
import json
import socket
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

from talkpipe_vault.apps import access_control, query, user_settings, vault_server

_REAL_RUNNING_INSTANCE_URL = query._running_instance_url


@pytest.fixture(autouse=True)
def _quiet_startup(monkeypatch):
    """Keep vault_server.main() deterministic and off the real port 8002.

    Without this, a vault really running on this machine would make main()
    short-circuit as "already running" or pick a different port.
    """
    monkeypatch.setattr(vault_server, "_running_instance_url", lambda h, p: None)
    monkeypatch.setattr(vault_server, "_port_in_use", lambda h, p: False)


@pytest.mark.parametrize(
    ("host", "expected"),
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


def test_module_form_opens_browser_by_default(monkeypatch):
    calls = {}
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    monkeypatch.setattr("sys.argv", ["query.py"])

    query.main()

    assert calls["open_browser"] is True


def test_module_form_takes_the_vault_server_command_line(tmp_path, monkeypatch):
    """``python -m talkpipe_vault.apps.query`` is documented as taking the same
    options as ``vault-server``; a folder of documents opens in folder mode
    and ``--dir`` adds another."""
    monkeypatch.setenv(user_settings.VAULT_HOME_ENV, str(tmp_path / "home"))
    calls = {}
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# a\n")
    papers = tmp_path / "papers"
    papers.mkdir()
    argv = ["query.py", str(notes), "--dir", str(papers), "--no-browser"]
    monkeypatch.setattr("sys.argv", argv)

    query.main()

    assert calls["folders"] == [str(notes), str(papers)]
    assert calls["vault_path"] == ""
    assert calls["open_browser"] is False
    assert calls["port"] == vault_server.DEFAULT_PORT


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


def test_resume_opens_most_recent_vault(tmp_path, monkeypatch):
    monkeypatch.setenv(user_settings.VAULT_HOME_ENV, str(tmp_path / "home"))
    recent = tmp_path / "recent-vault"
    recent.mkdir()
    user_settings.remember_vault(str(recent))
    calls = {}
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    monkeypatch.setattr(
        "sys.argv",
        ["vault-server", str(tmp_path / "default"), "--resume", "--no-browser"],
    )

    vault_server.main()

    assert calls["vault_path"] == str(recent)


def test_resume_skips_unusable_recents_and_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv(user_settings.VAULT_HOME_ENV, str(tmp_path / "home"))
    legacy = tmp_path / "legacy-vault"
    (legacy / "vector_vault").mkdir(parents=True)
    user_settings.remember_vault(str(tmp_path / "gone"))  # no longer exists
    user_settings.remember_vault(str(legacy))  # unsupported layout
    calls = {}
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    default = tmp_path / "default"
    monkeypatch.setattr(
        "sys.argv", ["vault-server", str(default), "--resume", "--no-browser"]
    )

    vault_server.main()

    assert calls["vault_path"] == str(default)


def test_resume_without_path_or_recents_starts_unselected(tmp_path, monkeypatch):
    monkeypatch.setenv(user_settings.VAULT_HOME_ENV, str(tmp_path / "home"))
    calls = {}
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    monkeypatch.setattr("sys.argv", ["vault-server", "--resume", "--no-browser"])

    vault_server.main()

    assert calls["vault_path"] == ""


def test_a_missing_path_says_it_becomes_a_new_empty_vault(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv(user_settings.VAULT_HOME_ENV, str(tmp_path / "home"))
    calls = {}
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    typo = tmp_path / "nots"
    monkeypatch.setattr("sys.argv", ["vault-server", str(typo), "--no-browser"])

    vault_server.main()

    out = capsys.readouterr().out
    assert calls["vault_path"] == str(typo)  # opened as before
    assert "new, empty named vault" in out
    assert "check the path" in out


def test_a_broken_path_fence_fails_before_the_banner(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(user_settings.VAULT_HOME_ENV, str(tmp_path / "home"))
    monkeypatch.setenv(access_control.DOCUMENT_ROOTS_ENV, str(tmp_path / "missing"))
    calls = {}
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    monkeypatch.setattr("sys.argv", ["vault-server", "--no-browser"])

    with pytest.raises(SystemExit) as excinfo:
        vault_server.main()

    captured = capsys.readouterr()
    assert excinfo.value.code == 1
    assert "Starting TalkPipe Vault" not in captured.out
    assert access_control.DOCUMENT_ROOTS_ENV in captured.err
    assert str(tmp_path / "missing") in captured.err
    assert not calls


def test_banner_lists_only_the_folders_that_exist(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(user_settings.VAULT_HOME_ENV, str(tmp_path / "home"))
    calls = {}
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    real = tmp_path / "notes"
    real.mkdir()
    typo = tmp_path / "nots"
    argv = ["vault-server", "--dir", str(typo), "--dir", str(real), "--no-browser"]
    monkeypatch.setattr("sys.argv", argv)

    vault_server.main()

    folders_line = next(
        line for line in capsys.readouterr().out.splitlines() if "Folders:" in line
    )
    assert str(real) in folders_line
    assert str(typo) not in folders_line
    # The app still gets both, and warns about the missing one when opening.
    assert calls["folders"] == [str(typo), str(real)]


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


# --- already-running detection ------------------------------------------------


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    payload: dict[str, str] = {}  # noqa: RUF012 - swapped per test via monkeypatch

    def do_GET(self):
        body = json.dumps(self.payload).encode()
        self.send_response(200 if self.path == "/api/health" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def health_server():
    """A loopback HTTP server answering /api/health with a configurable payload."""
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_running_instance_url_recognises_this_app(health_server, monkeypatch):
    from talkpipe_vault import DIST_NAME

    monkeypatch.setattr(_HealthHandler, "payload", {"app": DIST_NAME, "version": "1"})
    port = health_server.server_address[1]

    assert _REAL_RUNNING_INSTANCE_URL("127.0.0.1", port) == f"http://127.0.0.1:{port}/"


def test_running_instance_url_ignores_other_programs(health_server, monkeypatch):
    monkeypatch.setattr(_HealthHandler, "payload", {"app": "something-else"})
    port = health_server.server_address[1]

    assert _REAL_RUNNING_INSTANCE_URL("127.0.0.1", port) is None


def test_running_instance_url_when_nothing_listens():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    assert _REAL_RUNNING_INSTANCE_URL("127.0.0.1", port, timeout=0.5) is None


def test_main_already_running_opens_browser_and_exits(monkeypatch, capsys):
    """A second launch must not fail on the busy port: open the running one."""
    calls = {}
    opened = []
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    monkeypatch.setattr(
        vault_server, "_running_instance_url", lambda h, p: "http://127.0.0.1:8002/"
    )
    monkeypatch.setattr(vault_server.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr("sys.argv", ["vault-server"])

    vault_server.main()

    assert opened == ["http://127.0.0.1:8002/"]
    assert calls == {}
    out = capsys.readouterr().out
    assert "already running at http://127.0.0.1:8002/" in out
    assert "Starting TalkPipe Vault" not in out


def test_main_already_running_respects_no_browser(monkeypatch, capsys):
    calls = {}
    opened = []
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    monkeypatch.setattr(
        vault_server, "_running_instance_url", lambda h, p: "http://127.0.0.1:8002/"
    )
    monkeypatch.setattr(vault_server.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr("sys.argv", ["vault-server", "--no-browser"])

    vault_server.main()

    assert opened == []
    assert calls == {}
    assert "already running" in capsys.readouterr().out


class _Redirected:
    """What urlopen returns after following the add-folder redirect."""

    def __init__(self, url):
        self._url = url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_main_already_running_hands_folders_to_the_running_instance(
    monkeypatch, capsys, tmp_path
):
    """A folder given to a second launch is added to the instance already up."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.txt").write_text("a document\n")
    posted = []

    def fake_urlopen(request, timeout=0):
        posted.append((request.full_url, request.data))
        return _Redirected(
            "http://127.0.0.1:8002/documents?message=Searching+"
            + str(docs).replace("/", "%2F")
            + ".+Its+index+is+kept+in+the+folder."
        )

    monkeypatch.setattr(vault_server, "run_app", lambda **k: pytest.fail("ran"))
    monkeypatch.setattr(
        vault_server, "_running_instance_url", lambda h, p: "http://127.0.0.1:8002/"
    )
    monkeypatch.setattr(vault_server.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("sys.argv", ["vault-server", str(docs), "--no-browser"])

    vault_server.main()

    assert posted == [
        (
            "http://127.0.0.1:8002/folders/add",
            urllib.parse.urlencode({"folder_path": str(docs)}).encode(),
        )
    ]
    out = capsys.readouterr().out
    assert f"Searching {docs}. Its index is kept in the folder." in out


def test_main_already_running_says_a_vault_path_was_not_opened(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.setattr(vault_server, "run_app", lambda **k: pytest.fail("ran"))
    monkeypatch.setattr(
        vault_server, "_running_instance_url", lambda h, p: "http://127.0.0.1:8002/"
    )
    monkeypatch.setattr(
        vault_server.urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("a vault must not be posted as a folder"),
    )
    vault = tmp_path / "my-vault"
    monkeypatch.setattr("sys.argv", ["vault-server", str(vault), "--no-browser"])

    vault_server.main()

    out = capsys.readouterr().out
    assert f"{vault} was not opened" in out
    assert "Vaults & Documents page" in out


# --- port choice --------------------------------------------------------------


def test_main_uses_default_port_when_free(monkeypatch):
    calls = {}
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    monkeypatch.setattr("sys.argv", ["vault-server", "--no-browser"])

    vault_server.main()

    assert calls["port"] == 8002


def test_main_falls_back_when_default_port_is_held_by_another_program(
    monkeypatch, capsys
):
    calls = {}
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    monkeypatch.setattr(vault_server, "_port_in_use", lambda h, p: p == 8002)
    monkeypatch.setattr("sys.argv", ["vault-server", "--no-browser"])

    vault_server.main()

    assert calls["port"] == 8003
    out = capsys.readouterr().out
    assert "Port 8002 is in use by another program; using port 8003" in out
    assert "http://127.0.0.1:8003" in out


def test_main_explicit_port_in_use_still_fails(monkeypatch, capsys):
    calls = {}
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    monkeypatch.setattr(vault_server, "_port_in_use", lambda h, p: True)
    monkeypatch.setattr("sys.argv", ["vault-server", "--no-browser", "--port", "8002"])

    with pytest.raises(SystemExit) as excinfo:
        vault_server.main()

    assert excinfo.value.code == 1
    assert "already in use" in capsys.readouterr().err
    assert calls == {}


def test_main_explicit_free_port_is_used_as_given(monkeypatch):
    calls = {}
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    monkeypatch.setattr("sys.argv", ["vault-server", "--no-browser", "--port", "9000"])

    vault_server.main()

    assert calls["port"] == 9000


def test_main_reports_when_no_nearby_port_is_free(monkeypatch, capsys):
    calls = {}
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    monkeypatch.setattr(vault_server, "_port_in_use", lambda h, p: True)
    monkeypatch.setattr("sys.argv", ["vault-server", "--no-browser"])

    with pytest.raises(SystemExit) as excinfo:
        vault_server.main()

    assert excinfo.value.code == 1
    assert "8002-8022 are all in use" in capsys.readouterr().err
    assert calls == {}


def test_port_in_use_detects_a_real_listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert query._port_in_use("127.0.0.1", port) is True
    finally:
        listener.close()
    assert query._port_in_use("127.0.0.1", port) is False


# --- packaged icons (used by desktop launchers) --------------------------------


def test_packaged_icons_ship_next_to_the_favicon():
    """The PNG and ICO ship for desktop launchers of the vault."""
    static = Path(query.__file__).parent / "static"
    assert (static / "favicon.svg").is_file()
    assert (static / "icon-256.png").is_file()
    assert (static / "icon.ico").is_file()
