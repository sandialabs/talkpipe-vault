"""The Textual TUI driven with Pilot against a real sample vault.

Pipelines that are cheap run for real (model2vec embeddings, Whoosh); the
chat LLM is stubbed on the app state, as the web-app tests do.
"""

import time
from pathlib import Path

import pytest
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    OptionList,
    Select,
    Static,
    TextArea,
)

from talkpipe_vault.apps import user_settings
from talkpipe_vault.tui.app import (
    ConfirmScreen,
    DirectoryPickerScreen,
    MessageScreen,
    QuestionArea,
    ResultsPane,
    ScriptArea,
    VaultApp,
    _shorten_path,
    main,
)
from talkpipe_vault.tui.service import VaultService
from tests.conftest import build_docs_vault

SAMPLE_DOCS = Path(__file__).resolve().parents[1] / "sampledocs"
SIZE = (110, 36)

# The pilot tests are coroutines; the project runs pytest-asyncio in strict mode.
pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv(user_settings.VAULT_HOME_ENV, str(tmp_path / "home"))
    monkeypatch.delenv("TALKPIPE_VAULT_ROOT", raising=False)
    monkeypatch.delenv("TALKPIPE_DOCUMENT_ROOTS", raising=False)
    yield
    VaultService()._clear_vault()


@pytest.fixture(scope="module")
def sample_vault(tmp_path_factory):
    vault = tmp_path_factory.mktemp("vault")
    build_docs_vault(SAMPLE_DOCS / "*", vault)
    return str(vault)


async def _settle(pilot, rounds: int = 8) -> None:
    for _ in range(rounds):
        await pilot.pause(0.05)


async def _wait_workers(app, pilot, timeout: float = 120.0) -> None:
    """Wait for thread workers (not modal-blocked ones) to finish."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause(0.1)
        if not any(w.is_running and w.group != "modal" for w in app.workers):
            await _settle(pilot)
            return
    raise AssertionError("workers did not finish")


def _text(widget) -> str:
    return str(widget.content)


async def test_opens_vault_searches_and_shows_chunk(sample_vault):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        assert app.service.vault_path == sample_vault
        assert "3 chunks" in _text(app.query_one("#vault-facts", Static))
        assert app.query_one("#tabs").active == "tab-search"

        query = app.query_one("#search-query", Input)
        query.value = "what is the sample document about"
        query.focus()
        await pilot.press("enter")
        await _wait_workers(app, pilot)
        pane = app.query_one("#search-results", ResultsPane)
        assert len(pane.results) == 3
        assert "3 results" in _text(app.query_one("#search-note", Static))
        assert "SampleDocument" in _text(
            app.query_one("#search-results-detail-title", Static)
        )

        # Enter loads the full chunk into the detail pane.
        pane.option_list.focus()
        await pilot.press("enter")
        await _wait_workers(app, pilot)
        body = _text(app.query_one("#search-results-detail-body", Static))
        assert "Heading" in body
        assert "(Enter: full chunk" not in body

        # `o` opens the source-document dialog.
        await pilot.press("o")
        await _wait_workers(app, pilot)
        assert isinstance(app.screen, MessageScreen)
        await pilot.press("escape")
        await _settle(pilot)

        # Copy chunk / Copy All never fail.
        await pilot.press("c")
        app.query_one("#search-copy-all", Button).press()
        await _settle(pilot)


async def test_keyword_tab_builds_index_then_searches(sample_vault):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f4")
        await _settle(pilot)
        assert app.query_one("#tabs").active == "tab-keywords"
        assert "Build" in str(app.query_one("#kw-build", Button).label)
        app.query_one("#kw-build", Button).press()
        await _settle(pilot)
        for _ in range(200):
            await pilot.pause(0.1)
            if (
                not app.service.fulltext_status()["running"]
                and app._fulltext_timer is None
            ):
                break
        await _wait_workers(app, pilot)
        assert app.service.status()["keyword_search_enabled"] is True
        assert "keywords on" in _text(app.query_one("#vault-facts", Static))
        assert "Rebuild" in str(app.query_one("#kw-build", Button).label)
        assert not app.query_one("#ask-keyword", Checkbox).has_class("hidden")

        query = app.query_one("#kw-query", Input)
        query.value = "heading"
        query.focus()
        await pilot.press("enter")
        await _wait_workers(app, pilot)
        assert app.query_one("#kw-results", ResultsPane).results


async def test_ask_shows_answer_and_citations(sample_vault, monkeypatch):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        monkeypatch.setattr(
            app.service.state, "chat_pipeline", lambda q: f"The answer to '{q}'."
        )
        await pilot.press("f5")
        await _settle(pilot)
        app.query_one("#question", TextArea).load_text("What is this?")
        app.query_one("#question", TextArea).focus()
        await pilot.press("enter")  # Enter asks
        await _wait_workers(app, pilot)
        assert "The answer to 'What is this?'" in _text(
            app.query_one("#answer", Static)
        )
        assert "Answered by" in _text(app.query_one("#answer-meta", Static))
        assert len(app.query_one("#citations", ResultsPane).results) == 3
        app.query_one("#ask-copy", Button).press()
        await _settle(pilot)


async def test_ask_error_is_shown(sample_vault, monkeypatch):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)

        def boom(_q):
            raise RuntimeError("ollama connection refused")

        monkeypatch.setattr(app.service.state, "chat_pipeline", boom)
        app.query_one("#question", TextArea).load_text("hello")
        app.query_one("#ask-go", Button).press()
        await _wait_workers(app, pilot)
        assert "connection refused" in _text(app.query_one("#answer", Static))
        assert "Settings" in _text(app.query_one("#answer", Static))


async def test_starts_on_vault_tab_without_vault_and_creates_one(tmp_path):
    app = VaultApp(VaultService())
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        assert app.query_one("#tabs").active == "tab-vault"
        assert "no vault open" in _text(app.query_one("#vault-name", Static))
        # Typing a documents folder suggests a vault name.
        docs = tmp_path / "my-notes"
        docs.mkdir()
        app.query_one("#source-path", Input).value = str(docs)
        await _settle(pilot)
        assert app.query_one("#vault-path", Input).value.endswith("-vault")
        # Open (create) a vault by path.
        vault = tmp_path / "new-vault"
        app.query_one("#vault-path", Input).value = str(vault)
        app.query_one("#open-vault", Button).press()
        await _wait_workers(app, pilot)
        assert app.service.vault_path == str(vault)
        assert vault.is_dir()
        assert app.query_one("#recent-vaults", OptionList).option_count == 1


async def test_non_vault_folder_asks_for_confirmation(tmp_path):
    messy = tmp_path / "messy"
    messy.mkdir()
    (messy / "a.txt").write_text("x")
    app = VaultApp(VaultService())
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        app.query_one("#vault-path", Input).value = str(messy)
        app.query_one("#open-vault", Button).press()
        await _wait_workers(app, pilot)
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#cancel")
        await _settle(pilot)
        assert app.service.vault_path == ""


async def test_delete_recent_vault_confirms(tmp_path):
    victim = tmp_path / "victim"
    other = tmp_path / "other"
    service = VaultService()
    service.startup("")
    service.open_vault(str(victim))
    service.open_vault(str(other))
    app = VaultApp(service, vault_path=str(other))
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f2")
        await _settle(pilot)
        recents = app.query_one("#recent-vaults", OptionList)
        assert recents.option_count == 2
        recents.highlighted = 1  # victim (other is most recent)
        assert recents.get_option_at_index(1).id == str(victim)
        app.query_one("#delete-recent", Button).press()
        await _settle(pilot)
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#confirm")
        await _settle(pilot)
        assert not victim.exists()
        assert recents.option_count == 1


async def test_index_documents_with_progress(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.txt").write_text("Terminal interfaces are useful over ssh. " * 8)
    app = VaultApp(VaultService())
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        app.query_one("#source-path", Input).value = str(docs)
        app.query_one("#vault-path", Input).value = str(tmp_path / "vault")
        app.query_one("#index", Button).press()
        for _ in range(600):
            await pilot.pause(0.2)
            snap = app.service.index_status()
            if (
                app._index_timer is None
                and not snap["running"]
                and (snap["message"] or snap["error"])
            ):
                break
        await _wait_workers(app, pilot)
        snap = app.service.index_status()
        assert snap["error"] is None, snap
        progress = _text(app.query_one("#index-progress", Static))
        assert "chunk" in progress.lower() or "index" in progress.lower()
        assert app.service.status()["chunks"] >= 1
        # The summary reports the vault total, not just this run's chunks.
        assert f"now holds {app.service.status()['chunks']} chunk" in progress


async def test_directory_picker_lists_folders(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    app = VaultApp(VaultService())
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        app.query_one("#source-path", Input).value = str(tmp_path)
        app.query_one("#source-browse", Button).press()
        await _wait_workers(app, pilot)
        assert isinstance(app.screen, DirectoryPickerScreen)
        options = app.screen.query_one("#picker-list", OptionList)
        assert options.option_count == 2
        options.highlighted = 0
        await pilot.press("enter")  # descend into alpha
        await _wait_workers(app, pilot)
        assert app.screen.query_one("#picker-path", Input).value == str(
            tmp_path / "alpha"
        )
        await pilot.click("#choose")
        await _settle(pilot)
        assert app.query_one("#source-path", Input).value == str(tmp_path / "alpha")


async def test_settings_tab_saves_models_and_credentials(sample_vault):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f6")
        await _settle(pilot)
        assert "Embeddings" in _text(app.query_one("#config-status", Static))
        app.query_one("#chat-model", Input).value = "llama3.2"
        app.query_one("#rag-result-limit", Input).value = "4"
        app.query_one("#settings-save", Button).press()
        await _wait_workers(app, pilot)
        assert app.service.settings_view()["overrides"]["chat_model"] == "llama3.2"
        assert app.query_one("#rag-result-limit", Input).value == "4"

        app.query_one("#shingle-overlap", Input).value = "99"
        app.query_one("#shingle-size", Input).value = "5"
        app.query_one("#settings-save", Button).press()
        await _settle(pilot)
        assert any("smaller than" in str(n.message) for n in app._notifications)

        app.query_one(
            "#cred-ollama_server_url", Input
        ).value = "http://ollama.example:11434"
        app.query_one("#credentials-save", Button).press()
        await _wait_workers(app, pilot)
        creds = {c["key"]: c for c in app.service.settings_view()["credentials"]}
        assert creds["ollama_server_url"]["value"] == "http://ollama.example:11434"


async def test_retrieval_filter_dialog_validates_and_saves(sample_vault):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f2")
        await _settle(pilot)
        app.query_one("#filter", Button).press()
        await _settle(pilot)
        dialog = app.screen
        dialog.query_one("#filter-script", TextArea).load_text("INPUT FROM (")
        await pilot.click("#validate")
        await _settle(pilot)
        assert "Error" in _text(dialog.query_one("#filter-status", Static))
        dialog.query_one("#filter-script", TextArea).load_text(
            '| lambdaFilter[expression="score > 0.5"]'
        )
        dialog.query_one("#filter-enabled", Checkbox).value = True
        await pilot.click("#save")
        await _settle(pilot)
        assert app.service.status()["filter_active"] is True
        assert "filter on" in _text(app.query_one("#vault-facts", Static))
        assert not app.query_one("#search-filter", Checkbox).has_class("hidden")


async def test_help_and_compact_mode(sample_vault):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=(80, 20)) as pilot:
        await _wait_workers(app, pilot)
        assert app.screen.has_class("compact")
        await pilot.press("f1")
        await _settle(pilot)
        assert isinstance(app.screen, MessageScreen)
        await pilot.press("escape")
        await _settle(pilot)
        # Every tab's primary control is on screen at 80x20.
        for key, widget_id in (
            ("f2", "#index"),
            ("f3", "#search-go"),
            ("f4", "#kw-go"),
            ("f5", "#ask-go"),
        ):
            await pilot.press(key)
            await _settle(pilot)
            region = app.query_one(widget_id).region
            assert region.y >= 0, (widget_id, region)
            assert region.bottom <= 20, (widget_id, region)


async def test_settings_form_shows_effective_sources(sample_vault):
    """Nothing overridden: the Selects still show the provider that will be used."""
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        assert app.service.settings_view()["overrides"]["chat_source"] == ""
        models = app.service.settings_view()["models"]
        assert app.query_one("#chat-source", Select).value == models["chat_source"]
        assert (
            app.query_one("#embedding-source", Select).value
            == models["embedding_source"]
        )


async def test_settings_save_reprobes_configuration_status(sample_vault):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f6")
        app.query_one("#rag-result-limit", Input).value = "3"
        app.query_one("#settings-save", Button).press()
        await _wait_workers(app, pilot)
        status = _text(app.query_one("#config-status", Static))
        assert "(not probed)" not in status
        assert "Embeddings" in status
        app.query_one("#credentials-save", Button).press()
        await _wait_workers(app, pilot)
        assert "(not probed)" not in _text(app.query_one("#config-status", Static))


async def test_unopenable_vault_path_stays_on_screen(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder")
    bad = str(blocker / "vault")
    app = VaultApp(VaultService(), vault_path=bad)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        assert app.service.vault_path == ""
        assert app.query_one("#tabs").active == "tab-vault"
        assert app.query_one("#vault-path", Input).value == bad
        assert "Error opening vault" in _text(app.query_one("#index-progress", Static))


def test_text_areas_do_not_shadow_tab_keys():
    for cls in (QuestionArea, ScriptArea):
        # The merged map is what Textual consults (BINDINGS alone would miss
        # bindings inherited from TextArea).
        keys = set(cls._merged_bindings.key_to_bindings)
        assert not keys & {"f6", "f7"}, cls
        assert "ctrl+shift+left" in keys  # the rest of TextArea's keys survive


def test_shorten_path():
    assert _shorten_path("/short", 40) == "/short"
    assert _shorten_path("/a/very/long/path/to/some/vault", 12) == "…/some/vault"


def test_main_runs_app(monkeypatch, tmp_path):
    captured = {}

    class FakeApp:
        def __init__(self, service, *, vault_path, resume):
            captured.update(vault_path=vault_path, resume=resume, service=service)

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr("talkpipe_vault.tui.app.VaultApp", FakeApp)
    main([str(tmp_path / "v"), "--resume", "--show-source-paths"])
    assert captured["ran"]
    assert captured["resume"] is True
    assert captured["vault_path"] == str(tmp_path / "v")
    assert captured["service"].state.show_source_paths is True
