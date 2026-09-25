"""The Textual TUI driven with Pilot against a real sample vault.

Pipelines that are cheap run for real (model2vec embeddings, Whoosh); the
chat LLM is stubbed on the app state, as the web-app tests do.
"""

import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Markdown,
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
    RetrievalFilterScreen,
    ScriptArea,
    VaultApp,
    _plain_text_as_markdown,
    _shorten_path,
    main,
)
from talkpipe_vault.tui.service import VaultService
from tests.conftest import build_docs_vault

SAMPLE_DOCS = Path(__file__).resolve().parents[1] / "sampledocs"
SIZE = (110, 36)


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


def _answer_source(app) -> str:
    """The Markdown the answer pane is currently rendering."""
    return app.query_one("#answer", Markdown).source


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
        monkeypatch.setattr(app.service.state, "last_refresh_time", float("inf"))
        await pilot.press("f5")
        await _settle(pilot)
        app.query_one("#question", TextArea).load_text("What is this?")
        app.query_one("#question", TextArea).focus()
        await pilot.press("enter")  # Enter asks
        await _wait_workers(app, pilot)
        assert "The answer to 'What is this?'" in _answer_source(app)
        assert "Answered by" in _text(app.query_one("#answer-meta", Static))
        assert len(app.query_one("#citations", ResultsPane).results) == 3
        app.query_one("#ask-copy", Button).press()
        await _settle(pilot)


async def test_ask_renders_markdown_answer(sample_vault, monkeypatch):
    """LLM answers are Markdown: tables and headings get laid out, and the
    copy button still hands back the source text the model wrote."""
    answer = (
        "## Summary\n\n| Item | Count |\n|---|---|\n| apples | 3 |\n| pears | *5* |\n"
    )
    app = VaultApp(VaultService(), vault_path=sample_vault)
    copied: list[str] = []
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        monkeypatch.setattr(app.service.state, "chat_pipeline", lambda q: answer)
        monkeypatch.setattr(app.service.state, "last_refresh_time", float("inf"))
        monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
        app.query_one("#question", TextArea).load_text("How many?")
        app.query_one("#ask-go", Button).press()
        await _wait_workers(app, pilot)
        await _settle(pilot)
        markdown = app.query_one("#answer", Markdown)
        assert markdown.source == answer
        # Rendered as blocks (a heading and a table), not as one raw string.
        kinds = {type(block).__name__ for block in markdown.query("MarkdownBlock")}
        assert "MarkdownH2" in kinds
        assert "MarkdownTable" in kinds
        app.query_one("#ask-copy", Button).press()
        await _settle(pilot)
        assert copied == [answer]


async def test_plain_text_as_markdown_keeps_prose_verbatim():
    """Error text and hints go through the Markdown widget unchanged."""
    from markdown_it import MarkdownIt

    text = "Set TALKPIPE_OLLAMA_SERVER_URL in ~/.talkpipe.toml\n*not* a list: - x"
    html = MarkdownIt("gfm-like").render(_plain_text_as_markdown(text))
    assert "<em>" not in html
    assert "<li>" not in html
    assert "TALKPIPE_OLLAMA_SERVER_URL in ~/.talkpipe.toml" in html
    assert "*not* a list: - x" in html
    assert "<br" in html  # the newline survives as a hard line break


async def test_ask_error_is_shown(sample_vault, monkeypatch):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)

        def boom(_q):
            raise RuntimeError("ollama connection refused")

        monkeypatch.setattr(app.service.state, "chat_pipeline", boom)
        monkeypatch.setattr(app.service.state, "last_refresh_time", float("inf"))
        app.query_one("#question", TextArea).load_text("hello")
        app.query_one("#ask-go", Button).press()
        await _wait_workers(app, pilot)
        assert "connection refused" in _answer_source(app)
        assert "Settings" in _answer_source(app)


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


async def test_directory_picker_notes_an_empty_folder(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    app = VaultApp(VaultService())
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        app.query_one("#source-browse", Button).press()
        await _wait_workers(app, pilot)
        assert isinstance(app.screen, DirectoryPickerScreen)
        # The picker focuses its folder list after loading, so Enter would
        # descend into the highlighted sub-folder of the start directory
        # (whatever the host has there) rather than submit the typed path.
        path_input = app.screen.query_one("#picker-path", Input)
        path_input.value = str(empty)
        path_input.focus()
        await _settle(pilot)
        await pilot.press("enter")
        await _wait_workers(app, pilot)
        options = app.screen.query_one("#picker-list", OptionList)
        assert options.option_count == 1
        assert options.get_option_at_index(0).disabled
        assert "no sub-folders" in options.get_option_at_index(0).prompt


async def test_vault_suggestion_tracks_the_documents_path(tmp_path):
    app = VaultApp(VaultService())
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        source = app.query_one("#source-path", Input)
        vault = app.query_one("#vault-path", Input)
        # The path is built up a keystroke at a time; the suggestion has to
        # follow it, not freeze on the first change.
        (tmp_path / "n").mkdir()
        source.value = str(tmp_path / "n")
        await _settle(pilot)
        first = vault.value
        assert first
        (tmp_path / "notes").mkdir()
        source.value = str(tmp_path / "notes")
        await _settle(pilot)
        assert vault.value != first
        assert vault.value == app.service.suggest_vault_path(str(tmp_path / "notes"))
        # Once the user types their own vault path, further edits leave it be.
        vault.value = str(tmp_path / "chosen-vault")
        source.value = str(tmp_path / "n")
        await _settle(pilot)
        assert vault.value == str(tmp_path / "chosen-vault")


async def test_empty_vault_opens_on_the_vault_tab_with_a_hint(tmp_path):
    app = VaultApp(VaultService(), vault_path=str(tmp_path / "fresh-vault"))
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        assert app.service.vault_path == str(tmp_path / "fresh-vault")
        assert app.service.status()["chunks"] == 0
        assert app.query_one("#tabs").active == "tab-vault"
        assert "empty" in _text(app.query_one("#index-progress", Static)).lower()


async def test_config_status_refreshes_after_indexing(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.txt").write_text("Terminal interfaces are handy over ssh. " * 8)
    app = VaultApp(VaultService(), vault_path=str(tmp_path / "vault"))
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        app.query_one("#source-path", Input).value = str(docs)
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
        status = _text(app.query_one("#config-status", Static))
        # The embedding↔index check must reflect the run that just finished,
        # not still say the vault has nothing indexed.
        assert "No documents have been indexed" not in status
        assert "matches the one this vault was indexed with" in status


async def test_filter_validate_error_is_bounded(sample_vault):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f2")
        await _settle(pilot)
        app.query_one("#filter", Button).press()
        await _settle(pilot)
        app.screen.query_one("#filter-script", TextArea).load_text("| nosuchsegment")
        await pilot.click("#validate")
        await _settle(pilot)
        status = _text(app.screen.query_one("#filter-status", Static))
        # The real problem is kept; the full segment list can't push the
        # buttons off a 24-row screen.
        assert "not found" in status
        assert len(status) <= 240
        await pilot.press("escape")
        await _settle(pilot)


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

        def run(self, **_kwargs):
            captured["ran"] = True

    monkeypatch.setattr("talkpipe_vault.tui.app.VaultApp", FakeApp)
    main([str(tmp_path / "v"), "--resume", "--show-source-paths"])
    assert captured["ran"]
    assert captured["resume"] is True
    assert captured["vault_path"] == str(tmp_path / "v")
    assert captured["service"].state.show_source_paths is True


# --------------------------------------------------------------------------
# Second first-use review
# --------------------------------------------------------------------------


async def _index_and_wait(app, pilot) -> None:
    app.query_one("#index", Button).press()
    await _settle(pilot)
    for _ in range(600):
        await pilot.pause(0.1)
        if app._index_timer is None and not any(
            w.is_running and w.group != "modal" for w in app.workers
        ):
            break
    await _wait_workers(app, pilot)


async def test_confirm_dialog_focuses_cancel_so_enter_is_safe(tmp_path):
    victim = tmp_path / "victim"
    other = tmp_path / "other"
    service = VaultService()
    service.startup("")
    service.open_vault(str(victim))
    service.open_vault(str(other))  # the open vault is refused before any dialog
    app = VaultApp(service, vault_path=str(other))
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f2")
        await _settle(pilot)
        recents = app.query_one("#recent-vaults", OptionList)
        assert recents.get_option_at_index(1).id == str(victim)
        recents.highlighted = 1
        app.query_one("#delete-recent", Button).press()
        await _settle(pilot)
        assert isinstance(app.screen, ConfirmScreen)
        assert app.screen.focused is not None
        assert app.screen.focused.id == "cancel"
        await pilot.press("enter")
        await _settle(pilot)
        assert not isinstance(app.screen, ConfirmScreen)
        assert victim.is_dir()


async def test_overwrite_unticks_and_summary_explains_duplicates(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.txt").write_text("Terminal interfaces are useful over ssh. " * 8)
    app = VaultApp(VaultService())
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        app.query_one("#source-path", Input).value = str(docs)
        app.query_one("#vault-path", Input).value = str(tmp_path / "vault")
        await _index_and_wait(app, pilot)
        first = _text(app.query_one("#index-progress", Static))
        assert "Overwrite was off" not in first
        # Same folder again without Overwrite: the summary says the old chunks stayed.
        await _index_and_wait(app, pilot)
        second = _text(app.query_one("#index-progress", Static))
        assert "Overwrite was off" in second
        assert "twice" in second
        # A replace run unticks the box afterwards.
        app.query_one("#overwrite", Checkbox).value = True
        await _index_and_wait(app, pilot)
        assert "Overwrite was off" not in _text(
            app.query_one("#index-progress", Static)
        )
        assert app.query_one("#overwrite", Checkbox).value is False


async def test_resume_without_recent_vault_says_so():
    app = VaultApp(VaultService(), resume=True)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        assert app.query_one("#tabs").active == "tab-vault"
        assert "No recently used vault" in _text(
            app.query_one("#index-progress", Static)
        )


async def test_refused_open_stays_on_screen(tmp_path, monkeypatch):
    inside = tmp_path / "inside"
    inside.mkdir()
    monkeypatch.setenv("TALKPIPE_VAULT_ROOT", str(inside))
    app = VaultApp(VaultService())
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        app.query_one("#vault-path", Input).value = str(tmp_path / "outside")
        app.query_one("#open-vault", Button).press()
        await _wait_workers(app, pilot)
        assert app.service.vault_path == ""
        assert str(inside) in _text(app.query_one("#index-progress", Static))


async def test_results_are_numbered(sample_vault):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        query = app.query_one("#search-query", Input)
        query.value = "sample document"
        query.focus()
        await pilot.press("enter")
        await _wait_workers(app, pilot)
        options = app.query_one("#search-results", ResultsPane).option_list
        assert str(options.get_option_at_index(0).prompt).startswith("1. ")
        assert str(options.get_option_at_index(2).prompt).startswith("3. ")
        assert "Tab: scroll" in _text(
            app.query_one("#search-results-detail-body", Static)
        )


async def test_ask_error_clears_previous_citations(sample_vault, monkeypatch):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        monkeypatch.setattr(app.service.state, "chat_pipeline", lambda q: "fine")
        monkeypatch.setattr(app.service.state, "last_refresh_time", float("inf"))
        app.query_one("#question", TextArea).load_text("first")
        app.query_one("#ask-go", Button).press()
        await _wait_workers(app, pilot)
        assert app.query_one("#citations", ResultsPane).results

        def boom(_q):
            raise RuntimeError("ollama connection refused")

        monkeypatch.setattr(app.service.state, "chat_pipeline", boom)
        monkeypatch.setattr(app.service.state, "last_refresh_time", float("inf"))
        app.query_one("#question", TextArea).load_text("second")
        app.query_one("#ask-go", Button).press()
        await _wait_workers(app, pilot)
        assert "Error" in _text(app.query_one("#answer-meta", Static))
        assert app.query_one("#citations", ResultsPane).results == []


async def test_ask_tab_hints_at_keyword_boost_until_index_exists(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.txt").write_text("Keyword boost needs a full-text index. " * 4)
    app = VaultApp(VaultService())
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        # No vault: neither the checkbox nor the hint.
        assert app.query_one("#ask-keyword-hint", Static).has_class("hidden")
        app.query_one("#source-path", Input).value = str(docs)
        app.query_one("#vault-path", Input).value = str(tmp_path / "vault")
        await _index_and_wait(app, pilot)
        assert not app.query_one("#ask-keyword-hint", Static).has_class("hidden")
        assert app.query_one("#ask-keyword", Checkbox).has_class("hidden")
        assert "F4" in _text(app.query_one("#ask-keyword-hint", Static))


async def test_help_dialog_scrolls_and_enter_closes(sample_vault):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=(80, 24)) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f1")
        await _settle(pilot)
        assert isinstance(app.screen, MessageScreen)
        body = app.screen.query_one("#message-body")
        assert app.screen.focused is body
        await pilot.press("pagedown")
        await _settle(pilot)
        assert body.scroll_y > 0
        await pilot.press("enter")
        await _settle(pilot)
        assert not isinstance(app.screen, MessageScreen)


async def test_filter_dialog_shows_an_example(sample_vault):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f2")
        await _settle(pilot)
        app.query_one("#filter", Button).press()
        await _settle(pilot)
        statics = " ".join(_text(w) for w in app.screen.query(Static))
        assert "lambdaFilter" in statics
        assert "doc_id" in statics
        assert "lambdaFilter" in app.screen.query_one("#filter-script").placeholder
        await pilot.press("escape")
        await _settle(pilot)


async def test_filter_dialog_help_carries_the_web_directions(sample_vault):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f2")
        await _settle(pilot)
        app.query_one("#filter", Button).press()
        await _settle(pilot)
        app.screen.query_one("#filter-help", Button).press()
        await _settle(pilot)
        assert isinstance(app.screen, MessageScreen)
        body = " ".join(_text(w) for w in app.screen.query(Static))
        # Everything the web interface's "How to write a filter" section says.
        for phrase in (
            '"doc_id"',
            "item['score'] > 0.2",
            'isNotIn[field="document.content"',
            'isIn[field="document.source"',
            "case-sensitive",
            "INPUT FROM",
            "Strict",
        ):
            assert phrase in body
        await pilot.press("escape")
        await _settle(pilot)
        assert isinstance(app.screen, RetrievalFilterScreen)
        await pilot.press("escape")
        await _settle(pilot)


async def test_settings_empty_number_fields_are_valid(sample_vault):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f6")
        await _settle(pilot)
        for widget_id in ("#chunk-size", "#shingle-size", "#rag-result-limit"):
            field = app.query_one(widget_id, Input)
            assert field.value == ""
            assert field.is_valid


def test_config_status_wording_names_tabs():
    from talkpipe_vault.tui.app import _tui_wording

    assert _tui_wording("re-index (Vaults & Documents → Overwrite).") == (
        "re-index (Vault tab (F2) → Overwrite existing index)."
    )
    assert "Vault tab (F2) → Retrieval filter" in _tui_wording(
        "under Vaults & Documents → Retrieval filter for this vault."
    )


def test_help_text_reflows_and_lists_ctrl_c():
    from talkpipe_vault.tui.app import HELP_TEXT

    # The dialog wraps the text to whatever width the terminal has. Lines
    # pre-wrapped for one width re-wrap with orphans at a narrower one, so
    # every paragraph is one logical line: nothing starts with a hanging
    # indent, and a line ends only where a sentence or heading does.
    lines = [line for line in HELP_TEXT.splitlines() if line.strip()]
    assert not [line for line in lines if line.startswith(" ")]
    assert all(line.rstrip().endswith((".", ")", ":", "[/b]")) for line in lines), [
        line for line in lines if not line.rstrip().endswith((".", ")", ":", "[/b]"))
    ]
    assert "Ctrl+C" in HELP_TEXT
    assert "scroll" in HELP_TEXT.lower()


async def test_help_dialog_wraps_cleanly_at_sixty_columns(sample_vault):
    from talkpipe_vault.tui.app import HELP_TEXT

    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=(60, 20)) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f1")
        await _settle(pilot)
        assert isinstance(app.screen, MessageScreen)
        body = app.screen.query_one("#message-body").query_one(Static)
        region = body.content_region
        rendered = [
            strip.text.rstrip() for strip in body.render_lines(region.reset_offset)
        ]
        assert rendered
        assert any("F2 Vault" in line for line in rendered), rendered
        # No rendered line is a one- or two-word orphan left by double wrapping
        # (a heading is short on purpose).
        headings = {
            line[3:-4] for line in HELP_TEXT.splitlines() if line.startswith("[b]")
        }
        for line in rendered:
            words = line.split()
            orphan = 0 < len(words) <= 2 and not line.endswith((".", ")"))
            assert not orphan or line in headings, (line, rendered)


def _inside(app, widget) -> bool:
    region = widget.region
    return (
        region.x >= 0
        and region.y >= 0
        and region.right <= app.size.width
        and region.bottom <= app.size.height
        and region.height > 0
    )


async def test_small_terminal_keeps_every_button_on_screen(tmp_path):
    vault = tmp_path / "small-vault"
    app = VaultApp(VaultService(), vault_path=str(vault))
    async with app.run_test(size=(60, 16)) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f2")
        await _settle(pilot)
        for widget_id in ("#open-recent", "#delete-recent", "#filter"):
            button = app.query_one(widget_id, Button)
            button.scroll_visible(animate=False)
            await _settle(pilot)
            assert _inside(app, button), widget_id

        app.query_one("#source-browse", Button).press()
        await _settle(pilot)
        assert isinstance(app.screen, DirectoryPickerScreen)
        for widget_id in ("#choose", "#up", "#cancel", "#picker-list"):
            assert _inside(app, app.screen.query_one(widget_id)), widget_id
        await pilot.press("escape")
        await _settle(pilot)

        app.query_one("#filter", Button).press()
        await _settle(pilot)
        for widget_id in (
            "#filter-script",
            "#filter-enabled",
            "#filter-strict",
            "#save",
            "#validate",
            "#remove",
            "#filter-help",
            "#cancel",
        ):
            assert _inside(app, app.screen.query_one(widget_id)), widget_id
        await pilot.press("escape")
        await _settle(pilot)


async def test_recent_vault_click_selects_and_double_click_opens(tmp_path):
    victim = tmp_path / "victim-vault"
    other = tmp_path / "other-vault"
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
        assert recents.get_option_at_index(1).id == str(victim)
        # A single click on the second row only moves the highlight: the
        # vault stays as it was and the Vault tab stays put.
        await pilot.click(recents, offset=(2, 2))
        await _settle(pilot)
        assert recents.highlighted == 1
        assert app.service.vault_path == str(other)
        assert app.query_one("#tabs").active == "tab-vault"
        # The highlight is what Delete selected acts on.
        app.query_one("#delete-recent", Button).press()
        await _settle(pilot)
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#cancel")
        await _settle(pilot)
        # A double click opens the vault.
        await pilot.double_click(recents, offset=(2, 2))
        await _wait_workers(app, pilot)
        assert app.service.vault_path == str(victim)
        assert app.query_one("#tabs").active == "tab-search"


async def test_deleting_the_open_vault_refuses_before_confirming(tmp_path):
    vault = tmp_path / "open-vault"
    app = VaultApp(VaultService(), vault_path=str(vault))
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f2")
        await _settle(pilot)
        recents = app.query_one("#recent-vaults", OptionList)
        recents.highlighted = 0
        assert "(open)" in str(recents.get_option_at_index(0).prompt)
        app.query_one("#delete-recent", Button).press()
        await _settle(pilot)
        assert not isinstance(app.screen, ConfirmScreen)
        assert vault.exists()
        assert any("currently open" in n.message for n in app._notifications)


async def test_empty_query_gives_feedback(sample_vault):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f3")
        await _settle(pilot)
        await pilot.press("enter")
        await _settle(pilot)
        assert "Enter a query" in _text(app.query_one("#search-note", Static))
        await pilot.press("f4")
        await _settle(pilot)
        await pilot.press("enter")
        await _settle(pilot)
        assert "Enter a query" in _text(app.query_one("#kw-note", Static))


async def test_stale_full_text_index_is_called_out(tmp_path):
    vault = tmp_path / "stale-vault"
    build_docs_vault(SAMPLE_DOCS / "*", vault)
    service = VaultService()
    service.startup(str(vault))
    assert service.start_fulltext_index()["ok"]
    deadline = time.monotonic() + 60
    while service.fulltext_status()["running"] and time.monotonic() < deadline:
        time.sleep(0.1)
    newest = time.time() + 60
    for entry in (vault / "docs.lance").rglob("*"):
        if entry.is_file():
            os.utime(entry, (newest, newest))
    app = VaultApp(service, vault_path=str(vault))
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        assert "keywords out of date" in _text(app.query_one("#vault-facts", Static))
        await pilot.press("f4")
        await _settle(pilot)
        query = app.query_one("#kw-query", Input)
        query.value = "nothing-matches-this"
        query.focus()
        await pilot.press("enter")
        await _wait_workers(app, pilot)
        note = _text(app.query_one("#kw-note", Static))
        assert "0 results" in note
        assert "out of date" in note
        body = _text(app.query_one("#kw-results-detail-body", Static))
        assert "Rebuild full-text index" in body


async def test_config_status_shows_probe_detail(sample_vault, monkeypatch):
    """The detail line (timeout, cache path, exception text) must be visible."""
    monkeypatch.setattr(
        "talkpipe_vault.tui.service.VaultService.config_status",
        lambda self, **_kw: {
            "overall": "error",
            "checks": [
                {
                    "name": "Embeddings provider",
                    "status": "error",
                    "value": "model2vec / m",
                    "summary": "present locally but failed to produce a test embedding.",
                    "fix": "re-download it",
                    "detail": "probe did not finish within 20s",
                }
            ],
        },
    )
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        text = _text(app.query_one("#config-status", Static))
        assert "Detail: probe did not finish within 20s" in text


def test_prepare_process_creates_tqdm_lock_before_the_app(monkeypatch):
    """The lock (and multiprocessing's resource tracker) must exist before
    Textual owns the terminal; see prepare_process_for_textual."""
    from tqdm import tqdm

    from talkpipe_vault.tui.app import prepare_process_for_textual

    # tqdm only builds the lock when the attribute is absent.
    monkeypatch.delattr(tqdm, "_lock", raising=False)
    prepare_process_for_textual()
    assert getattr(tqdm, "_lock", None) is not None


def test_main_prepares_the_process_before_running(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        "talkpipe_vault.tui.app.prepare_process_for_textual",
        lambda: calls.append("prepared"),
    )

    class FakeApp:
        def __init__(self, service, *, vault_path, resume):
            calls.append("constructed")

        def run(self, **_kwargs):
            calls.append("ran")

    monkeypatch.setattr("talkpipe_vault.tui.app.VaultApp", FakeApp)
    main([str(tmp_path / "v")])
    assert calls == ["prepared", "constructed", "ran"]


async def test_startup_keeps_an_opening_line_until_the_vault_is_ready(tmp_path):
    vault = tmp_path / "slow-vault"
    app = VaultApp(VaultService(), vault_path=str(vault))
    async with app.run_test(size=SIZE) as pilot:
        # Before the worker finishes the header and the Vault tab say so.
        await pilot.pause(0.01)
        assert _text(app.query_one("#vault-name", Static)) == "opening…"
        await _wait_workers(app, pilot)
        assert _text(app.query_one("#vault-name", Static)).endswith("slow-vault")
        assert app.query_one("#tabs").active == "tab-vault"
        assert "This vault is empty" in _text(app.query_one("#index-progress", Static))


async def test_command_line_documents_folder_is_confirmed_first(tmp_path):
    docs = tmp_path / "notes"
    docs.mkdir()
    (docs / "a.md").write_text("# a\n")
    app = VaultApp(VaultService(), vault_path=str(docs))
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("escape")
        await _wait_workers(app, pilot)
        assert app.service.vault_path == ""
        assert app.query_one("#tabs").active == "tab-vault"
        assert app.query_one("#vault-path", Input).value == str(docs)
        assert "was not opened" in _text(app.query_one("#index-progress", Static))
        assert not (docs / "docs.lance").exists()


async def test_escape_cancels_waiting_for_an_answer(sample_vault, monkeypatch):
    import threading

    release = threading.Event()
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)

        def slow(_q):
            release.wait(10)
            return "late answer"

        monkeypatch.setattr(app.service.state, "chat_pipeline", slow)
        monkeypatch.setattr(app.service.state, "last_refresh_time", float("inf"))
        app.query_one("#question", TextArea).load_text("hello")
        app.query_one("#ask-go", Button).press()
        await _settle(pilot)
        assert app.ask_running()
        assert "Esc" in _text(app.query_one("#answer-meta", Static))
        await pilot.press("escape")
        await _settle(pilot)
        assert _text(app.query_one("#answer-meta", Static)) == "Cancelled"
        release.set()
        await _wait_workers(app, pilot)
        # The late result must not replace the cancellation.
        assert _text(app.query_one("#answer-meta", Static)) == "Cancelled"
        assert "late answer" not in _answer_source(app)


async def test_page_keys_in_the_question_box_scroll_the_answer(
    sample_vault, monkeypatch
):
    from textual.containers import VerticalScroll

    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        monkeypatch.setattr(
            app.service.state,
            "chat_pipeline",
            lambda _q: "\n\n".join(f"Paragraph {i}." for i in range(80)),
        )
        monkeypatch.setattr(app.service.state, "last_refresh_time", float("inf"))
        question = app.query_one("#question", TextArea)
        question.load_text("hello")
        question.focus()
        await pilot.press("enter")
        await _wait_workers(app, pilot)
        pane = app.query_one("#answer-pane", VerticalScroll)
        assert pane.scroll_y == 0
        await pilot.press("pagedown")
        await _settle(pilot)
        assert pane.scroll_y > 0
        await pilot.press("pageup")
        await _settle(pilot)
        assert pane.scroll_y == 0


async def test_quit_asks_first_while_indexing(sample_vault, monkeypatch):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        monkeypatch.setattr(
            app.service,
            "index_status",
            lambda: {
                "running": True,
                "phase": "indexing",
                "source": "/docs",
                "vault_path": sample_vault,
                "files_done": 3,
                "total_files": 40,
            },
        )
        await pilot.press("ctrl+q")
        await _settle(pilot)
        assert isinstance(app.screen, ConfirmScreen)
        assert "3/40" in app.screen._message
        await pilot.press("escape")
        await _settle(pilot)
        assert not app._exit
        assert not isinstance(app.screen, ConfirmScreen)


async def test_source_dialog_shows_any_text_file(sample_vault, tmp_path):
    csv = tmp_path / "rows.csv"
    csv.write_text("item,qty\nsensor,12\n")
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        pane = app.query_one("#search-results", ResultsPane)
        pane._show_source({"ok": True, "path": str(csv), "is_text": True})
        await _settle(pilot)
        assert isinstance(app.screen, MessageScreen)
        assert "sensor,12" in app.screen._body
        await pilot.press("escape")
        await _settle(pilot)
        pane._show_source({"ok": True, "path": str(csv), "is_text": False})
        await _settle(pilot)
        assert "cannot be shown" in app.screen._body
        await pilot.press("escape")


async def test_indexing_summary_counts_files_without_content(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.md").write_text("# Note\n\nSomething worth indexing here.\n")
    (docs / "empty.md").write_text("")
    (docs / "blob.bin").write_bytes(bytes(range(256)) * 4)
    vault = tmp_path / "vault"
    app = VaultApp(VaultService(), vault_path=str(vault))
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        app.query_one("#source-path", Input).value = str(docs)
        await _index_and_wait(app, pilot)
        text = _text(app.query_one("#index-progress", Static))
        assert "The vault now holds" in text
        assert "of the matched files had no readable content" in text
        assert "2 of the matched files" in text


async def test_full_chunk_is_labelled_in_the_detail_title(sample_vault):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        query = app.query_one("#search-query", Input)
        query.value = "sample document"
        query.focus()
        await pilot.press("enter")
        await _wait_workers(app, pilot)
        title = app.query_one("#search-results-detail-title", Static)
        assert "full chunk" not in _text(title)
        app.query_one("#search-results", ResultsPane).option_list.focus()
        await pilot.press("enter")
        await _wait_workers(app, pilot)
        assert "full chunk" in _text(title)


async def test_header_keeps_the_vault_name_at_80_columns(tmp_path):
    vault = tmp_path / "a-rather-long-parent-folder-name" / "budget-notes-vault"
    app = VaultApp(VaultService(), vault_path=str(vault))
    async with app.run_test(size=(80, 24)) as pilot:
        await _wait_workers(app, pilot)
        shown = _text(app.query_one("#vault-name", Static))
        assert shown.endswith("budget-notes-vault")
        assert len(shown) <= app.query_one("#vault-name", Static).content_size.width


def test_run_app_leaves_without_waiting_for_blocked_threads(monkeypatch):
    from talkpipe_vault.tui import app as app_module

    calls: list[Any] = []
    monkeypatch.setattr(app_module.os, "_exit", lambda code: calls.append(code))

    class FakeApp:
        def run(self, *, loop):
            calls.append(("run", loop))

    stuck = threading.Thread(target=lambda: None)
    monkeypatch.setattr(app_module, "_blocked_worker_threads", lambda: [stuck])
    app_module.run_app(FakeApp())  # type: ignore[arg-type]  # duck-typed stand-in
    assert calls[0][0] == "run"
    assert calls[-1] == 0

    calls.clear()
    monkeypatch.setattr(app_module, "_blocked_worker_threads", list)
    app_module.run_app(FakeApp())  # type: ignore[arg-type]  # duck-typed stand-in
    assert calls == [("run", calls[0][1])]


def _service_threads():
    from talkpipe_vault.tui.app import SERVICE_THREAD_NAME_PREFIX

    return [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith(SERVICE_THREAD_NAME_PREFIX) and thread.is_alive()
    ]


def test_run_app_releases_idle_service_threads_at_once(monkeypatch):
    """Threads that ran finished service calls must not delay quitting.

    Every thread worker runs on the loop's default executor, whose threads
    stay alive (and are not daemons) after their call returns. Quitting used
    to wait a full grace period for each of them, so a session's worth of
    searches and asks turned Ctrl+Q into a stall of several seconds — a
    hang, from the user's chair.
    """
    from talkpipe_vault.tui import app as app_module

    exits: list[int] = []
    monkeypatch.setattr(app_module.os, "_exit", exits.append)
    seen: list[str] = []

    class FakeApp:
        def run(self, *, loop):
            # Two completed service calls, as a search then an Ask would leave.
            for _ in range(2):
                loop.run_until_complete(
                    loop.run_in_executor(None, threading.current_thread)
                )
            seen.extend(thread.name for thread in _service_threads())

    started = time.monotonic()
    app_module.run_app(FakeApp())  # type: ignore[arg-type]  # duck-typed stand-in
    elapsed = time.monotonic() - started

    assert seen, "service calls should run on the named executor"
    assert exits == []
    assert not _service_threads()
    assert elapsed < app_module.BLOCKED_THREAD_GRACE_SECONDS / 2


def test_run_app_shares_one_grace_period_across_blocked_threads(monkeypatch):
    from talkpipe_vault.tui import app as app_module

    exits: list[int] = []
    monkeypatch.setattr(app_module.os, "_exit", exits.append)
    monkeypatch.setattr(app_module, "BLOCKED_THREAD_GRACE_SECONDS", 0.5)
    release = threading.Event()
    inside = threading.Barrier(3)

    def blocked_call():
        inside.wait(5)
        release.wait(10)

    class FakeApp:
        def run(self, *, loop):
            # Two calls still blocked (say, in an LLM request) when quitting.
            for _ in range(2):
                loop.run_in_executor(None, blocked_call)
            inside.wait(5)

    started = time.monotonic()
    try:
        app_module.run_app(FakeApp())  # type: ignore[arg-type]  # duck-typed stand-in
        elapsed = time.monotonic() - started
    finally:
        release.set()
        for thread in _service_threads():
            thread.join(5)

    assert exits == [0]
    assert 0.5 <= elapsed < 1.0


def test_blocked_worker_threads_ignores_daemons_and_self():
    from talkpipe_vault.tui.app import _blocked_worker_threads

    started = threading.Event()
    release = threading.Event()

    def wait():
        started.set()
        release.wait(5)

    daemon = threading.Thread(target=wait, daemon=True)
    worker = threading.Thread(target=wait)
    daemon.start()
    worker.start()
    started.wait(5)
    try:
        blocked = _blocked_worker_threads()
        assert worker in blocked
        assert daemon not in blocked
        assert threading.current_thread() not in blocked
    finally:
        release.set()
        worker.join()
        daemon.join()


async def test_shift_tab_from_the_top_of_settings_reaches_the_ollama_url(
    sample_vault,
):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f6")
        await _settle(pilot)
        assert app.focused is not None
        assert app.focused.id == "config-retest"
        await pilot.press("shift+tab")
        await _settle(pilot)
        assert app.focused is not None
        assert app.focused.id == "cred-ollama_server_url"
        # Elsewhere Shift+Tab keeps its ordinary meaning.
        await pilot.press("shift+tab")
        await _settle(pilot)
        assert app.focused.id != "cred-ollama_server_url"


async def test_saving_a_bare_host_port_completes_the_ollama_url(sample_vault):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f6")
        await _settle(pilot)
        app.query_one("#cred-ollama_server_url", Input).value = "ollama.example:11434"
        app.query_one("#credentials-save", Button).press()
        await _settle(pilot)
        # Check the toast before waiting out the live probe that Save starts:
        # it expires after Textual's default five seconds, and the probe can
        # take longer than that on a slow runner.
        assert any("Added http://" in str(n.message) for n in app._notifications)
        await _wait_workers(app, pilot)
        creds = {c["key"]: c for c in app.service.settings_view()["credentials"]}
        assert creds["ollama_server_url"]["value"] == "http://ollama.example:11434"
        assert app.query_one("#cred-ollama_server_url", Input).value == (
            "http://ollama.example:11434"
        )

        app.query_one("#cred-ollama_server_url", Input).value = "ftp://ollama.example"
        app.query_one("#credentials-save", Button).press()
        await _settle(pilot)
        assert any("not an http(s) URL" in str(n.message) for n in app._notifications)
        creds = {c["key"]: c for c in app.service.settings_view()["credentials"]}
        assert creds["ollama_server_url"]["value"] == "http://ollama.example:11434"


async def test_a_vault_deleted_underneath_the_app_is_reported(tmp_path):
    import shutil

    vault = tmp_path / "vault"
    build_docs_vault(SAMPLE_DOCS / "*", vault)
    app = VaultApp(VaultService(), vault_path=str(vault))
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        assert app.service.status()["chunks"] > 0
        shutil.rmtree(vault)
        await pilot.press("f3")
        app.query_one("#search-query", Input).value = "python"
        app.query_one("#search-go", Button).press()
        await _wait_workers(app, pilot)
        note = _text(app.query_one("#search-note", Static))
        assert "no longer on disk" in note, note
        await pilot.press("ctrl+r")
        await _wait_workers(app, pilot)
        assert any("no longer on disk" in str(n.message) for n in app._notifications)


async def test_question_box_grows_with_a_long_question(sample_vault):
    app = VaultApp(VaultService(), vault_path=sample_vault)
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        await pilot.press("f5")
        await _settle(pilot)
        box = app.query_one("#question", TextArea)
        before = box.size.height
        box.text = "tell me about " + "deployment and testing and " * 30
        await _settle(pilot)
        assert box.size.height > before
        assert box.size.height <= 7


async def test_path_fence_error_stays_on_the_vault_tab(tmp_path, monkeypatch):
    monkeypatch.setenv("TALKPIPE_VAULT_ROOT", str(tmp_path / "missing"))
    app = VaultApp(VaultService())
    async with app.run_test(size=SIZE) as pilot:
        await _wait_workers(app, pilot)
        line = _text(app.query_one("#index-progress", Static))
        assert "TALKPIPE_VAULT_ROOT" in line, line
        assert "missing" in line, line


def test_run_app_reports_a_crash_before_the_fast_exit(monkeypatch, capsys):
    import threading

    from talkpipe_vault.tui import app as app_module

    calls: list[Any] = []
    monkeypatch.setattr(app_module.os, "_exit", lambda code: calls.append(code))
    monkeypatch.setattr(app_module, "BLOCKED_THREAD_GRACE_SECONDS", 0.01)

    class CrashingApp:
        def run(self, *, loop):
            raise RuntimeError("boom")

    stuck = threading.Thread(target=lambda: None)
    monkeypatch.setattr(app_module, "_blocked_worker_threads", lambda: [stuck])
    with pytest.raises(RuntimeError):
        app_module.run_app(CrashingApp())  # type: ignore[arg-type]  # stand-in
    assert calls == [1]
    assert "boom" in capsys.readouterr().err
