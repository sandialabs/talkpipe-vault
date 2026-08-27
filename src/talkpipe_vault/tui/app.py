"""The TalkPipe Vault terminal application (Textual).

One screen with five tabs — Vault, Search, Keywords, Ask, Settings — mirroring
the web interface's pages, plus a header strip with the open vault's facts.
Every pipeline call runs in a worker thread through
:class:`talkpipe_vault.tui.service.VaultService`, so the interface stays
responsive while embeddings, LLM calls, and index builds run.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, cast

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, ScreenResultType
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Input,
    Label,
    Markdown,
    OptionList,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)
from textual.widgets.option_list import Option

from talkpipe_vault.tui.service import VaultService

TABS = [
    ("tab-vault", "Vault", "f2"),
    ("tab-search", "Search", "f3"),
    ("tab-keywords", "Keywords", "f4"),
    ("tab-ask", "Ask", "f5"),
    ("tab-settings", "Settings", "f6"),
]

# Below this many rows the screen gets the `compact` class: buttons shrink to
# one line and secondary panes give up their minimum heights.
COMPACT_ROWS = 26

HELP_TEXT = """\
[b]TalkPipe Vault — keyboard reference[/b]
(This text scrolls: PageDown / arrow keys, or Tab into it.)

[b]Tabs[/b]
  F2  Vault: open/create a vault, index documents, recent vaults,
      retrieval filter. Index documents adds to the open vault; tick
      "Overwrite existing index" to replace it (re-indexing the same
      folder without it duplicates chunks). Adding documents does not
      update the full-text index — rebuild it on the Keywords tab.
  F3  Search (semantic)        F4  Keywords (full-text)
  F5  Ask: question answering with citations. Enter in the question
      box asks; the box wraps long questions.
  F6  Settings: models, connections & credentials, configuration
      status.
  Tab / Shift+Tab move between fields and buttons inside a tab.

[b]Results (Search, Keywords, Ask citations)[/b]
  Up/Down   move through results; the detail pane follows
  Enter     load the full chunk text into the detail pane
  o         show where the source document is on disk (view it if
            text)
  c         copy the highlighted chunk to the clipboard
  Copy All  copies every result (button)
  Tab       into the detail pane (or Ask's answer pane), then
            Up/Down or PageUp/PageDown scroll long text
  Exact words: semantic Search ranks by meaning, so a rare word can
  land below unrelated notes — the Keywords tab finds it exactly
  (build its index there first).
  When the vault's retrieval filter is enabled, an "Apply retrieval
  filter" checkbox appears on Search and Keywords; Ask always applies
  it.

[b]Everywhere[/b]
  Ctrl+R  reload the vault and settings (after indexing or changing
          ~/.talkpipe.toml or TALKPIPE_* variables outside this app)
  F1      this help        Ctrl+Q  quit        Esc  close a dialog
  Ctrl+C  does not quit (it only reminds you of Ctrl+Q), so a stray
          Ctrl+C never loses a long answer.

[b]Where things live[/b]
  Recent vaults, model settings and credentials: the same files the
  web interface uses (TALKPIPE_VAULT_HOME, default ~/.talkpipe-vault),
  so both interfaces share every vault and setting.
"""


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------


class _Dialog(ModalScreen[ScreenResultType]):
    """Modal base: mirrors the main screen's `compact` class.

    The class is set on the app's default screen only, so without this a
    dialog on a 24-row terminal keeps three-row buttons that get clipped to
    unlabelled bars.
    """

    def on_mount(self) -> None:
        self.set_class(self.app.size.height < COMPACT_ROWS, "compact")


class MessageScreen(_Dialog[None]):
    """Scrollable text with an OK button (help, chunk text, documents)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss", "Close"),
        # Enter closes too, unless a button has focus (Button consumes it).
        Binding("enter", "dismiss", "Close", show=False),
    ]

    def __init__(
        self,
        title: str,
        body: str,
        *,
        markup: bool = True,
        copy_text: str | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._markup = markup
        self._copy_text = copy_text

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog dialog-wide dialog-tall"):
            yield Label(self._title, classes="dialog-title")
            with VerticalScroll(classes="dialog-body", id="message-body"):
                yield Static(self._body, markup=self._markup)
            with Horizontal(classes="dialog-buttons"):
                if self._copy_text is not None:
                    yield Button("Copy", id="copy")
                yield Button("OK", variant="primary", id="ok")

    def on_mount(self) -> None:
        super().on_mount()
        # Long text (help, a whole document) is cut off on small terminals;
        # with the scroll area focused the arrow and page keys scroll it at
        # once instead of doing nothing on the OK button.
        self.query_one("#message-body", VerticalScroll).focus()

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#copy")
    def _copy(self) -> None:
        self.app.copy_to_clipboard(self._copy_text or "")
        self.notify("Copied to the clipboard.")


class ConfirmScreen(_Dialog[bool]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self, title: str, message: str, *, confirm_label: str = "Confirm"
    ) -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._title, classes="dialog-title")
            yield Static(self._message, classes="dialog-body", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button(self._confirm_label, variant="error", id="confirm")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        super().on_mount()
        # These confirm irreversible actions (delete a vault, create one among
        # documents); Enter straight after the button press must not do them.
        self.query_one("#cancel", Button).focus()

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)


class DirectoryPickerScreen(_Dialog[str | None]):
    """Folder picker with the same rules as the web dialog (path fences)."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, service: VaultService, start: str = "") -> None:
        super().__init__()
        self._service = service
        self._start = start
        self._current = ""
        self._parent_path: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog dialog-wide dialog-tall"):
            yield Label("Choose a folder", classes="dialog-title")
            yield Input(value=self._start, placeholder="Path", id="picker-path")
            yield Static("", id="picker-message", classes="status-line")
            yield OptionList(id="picker-list")
            yield Static(
                "Enter opens the highlighted folder · Use this folder chooses "
                "the path shown above",
                classes="muted dialog-help",
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Use this folder", variant="primary", id="choose")
                yield Button("Up", id="up")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        super().on_mount()
        self._load(self._start)

    @work(thread=True, exclusive=True, group="picker")
    def _load(self, path: str) -> None:
        listing = self._service.list_directories(path)
        self.app.call_from_thread(self._show, listing)

    def _show(self, listing: dict[str, Any]) -> None:
        message = self.query_one("#picker-message", Static)
        if not listing.get("ok"):
            message.update(str(listing.get("error") or "Could not read that folder."))
            message.set_classes("status-line error")
            return
        message.update("")
        message.set_classes("status-line")
        self._current = str(listing.get("path") or "")
        self._parent_path = listing.get("parent")
        self.query_one("#picker-path", Input).value = self._current
        options = self.query_one("#picker-list", OptionList)
        options.clear_options()
        sep = str(listing.get("sep") or "/")
        for name in listing.get("directories") or []:
            full = (
                name if not self._current else f"{self._current.rstrip(sep)}{sep}{name}"
            )
            options.add_option(Option(name, id=full))
        if options.option_count:
            options.highlighted = 0
        options.focus()

    @on(Input.Submitted, "#picker-path")
    def _typed(self, event: Input.Submitted) -> None:
        self._load(event.value.strip())

    @on(OptionList.OptionSelected, "#picker-list")
    def _descend(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self._load(str(event.option.id))

    @on(Button.Pressed, "#up")
    def _up(self) -> None:
        if self._parent_path is not None:
            self._load(self._parent_path)

    @on(Button.Pressed, "#choose")
    def _choose(self) -> None:
        typed = self.query_one("#picker-path", Input).value.strip()
        self.dismiss(typed or self._current or None)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


FILTER_EXAMPLE = (
    "| lambdaFilter[expression=\"'draft' not in "
    "item['document'].get('content', '').lower()\"]"
)


class RetrievalFilterScreen(_Dialog[dict[str, Any] | None]):
    """Edit the open vault's retrieval filter script (web: Vaults & Documents)."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, view: dict[str, Any]) -> None:
        super().__init__()
        self._view = view

    def compose(self) -> ComposeResult:
        v = self._view
        with Vertical(classes="dialog dialog-wide dialog-tall"):
            yield Label("Retrieval filter for this vault", classes="dialog-title")
            yield Static(
                "A ChatterLang script applied to retrieved chunks before they "
                "reach Ask and the search pages. Stored inside the vault; the "
                "enable/strict flags are stored on this machine.",
                classes="muted dialog-help",
            )
            yield Static(
                "Each result is {doc_id, score, document} — `item` in lambda/"
                f"lambdaFilter expressions — e.g. {FILTER_EXAMPLE}",
                classes="muted dialog-help",
                markup=False,
            )
            yield ScriptArea(
                v.get("script") or "",
                id="filter-script",
                classes="dialog-textarea",
                placeholder=FILTER_EXAMPLE,
            )
            with Vertical(classes="checkbox-column"):
                yield Checkbox(
                    "Enabled on this machine",
                    bool(v.get("enabled")),
                    id="filter-enabled",
                )
                yield Checkbox(
                    "Strict (fail instead of falling back)",
                    bool(v.get("strict")),
                    id="filter-strict",
                )
            yield Static(
                f"Error: {v['error']}" if v.get("error") else "",
                id="filter-status",
                classes="status-line error",
                markup=False,
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Validate", id="validate")
                yield Button("Remove", variant="error", id="remove")
                yield Button("Cancel", id="cancel")

    def _values(self, action: str) -> dict[str, Any]:
        return {
            "action": action,
            "script": self.query_one("#filter-script", TextArea).text,
            "enabled": self.query_one("#filter-enabled", Checkbox).value,
            "strict": self.query_one("#filter-strict", Checkbox).value,
        }

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.dismiss(self._values("save"))

    @on(Button.Pressed, "#remove")
    def _remove(self) -> None:
        self.dismiss(self._values("remove"))

    @on(Button.Pressed, "#validate")
    def _validate(self) -> None:
        app = cast("VaultApp", self.app)
        result = app.service.save_filter(**self._values("validate"))
        status = self.query_one("#filter-status", Static)
        status.update(
            result["message"] if result["ok"] else f"Error: {result['error']}"
        )
        status.set_classes(
            "status-line success" if result["ok"] else "status-line error"
        )

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ScriptArea(TextArea, inherit_bindings=False):
    """TextArea without its F6/F7 bindings (select line/all).

    Those keys shadow the app's tab bindings, so the footer reorders whenever
    a text area has focus. ``inherit_bindings=False`` because Textual merges
    a subclass's BINDINGS with its parents' otherwise.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        b for b in TextArea.BINDINGS if getattr(b, "key", "") not in ("f6", "f7")
    ]


class QuestionArea(ScriptArea):
    """The Ask box: Enter submits the question instead of inserting a newline.

    TextArea inserts the newline in its own key handler, before any binding
    is consulted, so the interception has to happen here.
    """

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            cast("VaultApp", self.app).ask_current_question()
            return
        await super()._on_key(event)


# --------------------------------------------------------------------------
# Result list + detail pane (shared by Search, Keywords, Ask citations)
# --------------------------------------------------------------------------


class ResultsPane(Horizontal):
    """A list of result rows with a detail pane that follows the highlight."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("o", "open_source", "Source doc", show=False),
        Binding("c", "copy_chunk", "Copy chunk", show=False),
    ]

    def __init__(self, *, id: str, empty_text: str) -> None:
        super().__init__(id=id, classes="results-pane")
        self.results: list[dict[str, Any]] = []
        self._empty_text = empty_text
        self._chunks: dict[int, str] = {}

    def compose(self) -> ComposeResult:
        yield OptionList(id=f"{self.id}-list", classes="results-list")
        with VerticalScroll(id=f"{self.id}-detail", classes="result-detail"):
            yield Static(
                "", id=f"{self.id}-detail-title", classes="result-detail-title"
            )
            yield Static(self._empty_text, id=f"{self.id}-detail-body", markup=False)

    @property
    def option_list(self) -> OptionList:
        return self.query_one(f"#{self.id}-list", OptionList)

    def show_results(self, results: list[dict[str, Any]], *, empty_text: str) -> None:
        self.results = results
        self._chunks = {}
        options = self.option_list
        options.clear_options()
        for index, result in enumerate(results):
            score = f"  [{result['score']}]" if result.get("score") else ""
            # Numbered: several chunks of one file otherwise read as identical rows.
            options.add_option(
                Option(f"{index + 1}. {result['filename']}{score}", id=str(index))
            )
        if results:
            options.highlighted = 0
            self._show_detail(0)
        else:
            self._set_detail("", empty_text)

    def _set_detail(self, title: str, body: str) -> None:
        self.query_one(f"#{self.id}-detail-title", Static).update(title)
        self.query_one(f"#{self.id}-detail-body", Static).update(body)

    def highlighted_index(self) -> int | None:
        index = self.option_list.highlighted
        return index if index is not None and index < len(self.results) else None

    def _show_detail(self, index: int) -> None:
        result = self.results[index]
        title = result["filename"]
        if result.get("score"):
            title += f"  ·  score {result['score']}"
        body = self._chunks.get(index) or result.get("snippet", "")
        path = result.get("path")
        if path and cast("VaultApp", self.app).service.status()["show_source_paths"]:
            body = f"{path}\n\n{body}"
        if index not in self._chunks:
            body += (
                "\n\n(Enter: full chunk · o: source document · c: copy · "
                "Tab: scroll this pane)"
            )
        self._set_detail(title, body)

    @on(OptionList.OptionHighlighted)
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == f"{self.id}-list" and event.option.id is not None:
            self._show_detail(int(str(event.option.id)))

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == f"{self.id}-list" and event.option.id is not None:
            self._load_chunk(int(str(event.option.id)))

    @work(thread=True, group="chunk")
    def _load_chunk(self, index: int) -> None:
        result = self.results[index]
        app = cast("VaultApp", self.app)
        outcome = app.service.chunk_text(
            result["lookup_path"], result.get("snippet", "")
        )
        self.app.call_from_thread(self._apply_chunk, index, outcome)

    def _apply_chunk(self, index: int, outcome: dict[str, Any]) -> None:
        if not outcome["ok"]:
            self.notify(outcome["error"], severity="error")
            return
        self._chunks[index] = str(outcome["content"])
        if self.highlighted_index() == index:
            self._show_detail(index)

    def action_copy_chunk(self) -> None:
        index = self.highlighted_index()
        if index is None:
            self.notify("Highlight a result first.", severity="warning")
            return
        text = self._chunks.get(index) or self.results[index].get("snippet", "")
        self.app.copy_to_clipboard(text)
        self.notify("Chunk copied to the clipboard.")

    def copy_all(self) -> None:
        if not self.results:
            self.notify("No results to copy.", severity="warning")
            return
        lines = []
        for index, result in enumerate(self.results):
            lines.append(f"{index + 1}. {result['filename']}")
            if result.get("score"):
                lines.append(f"   score: {result['score']}")
            lines.append(f"   {self._chunks.get(index) or result.get('snippet', '')}")
            lines.append("")
        self.app.copy_to_clipboard("\n".join(lines))
        self.notify(f"Copied {len(self.results)} results to the clipboard.")

    def action_open_source(self) -> None:
        index = self.highlighted_index()
        if index is None:
            self.notify("Highlight a result first.", severity="warning")
            return
        self._resolve_source(index)

    @work(thread=True, group="source")
    def _resolve_source(self, index: int) -> None:
        app = cast("VaultApp", self.app)
        outcome = app.service.source_file(self.results[index]["lookup_path"])
        self.app.call_from_thread(self._show_source, outcome)

    def _show_source(self, outcome: dict[str, Any]) -> None:
        if not outcome["ok"]:
            self.notify(outcome["error"], severity="error", timeout=8)
            return
        path = Path(str(outcome["path"]))
        body = f"Source document:\n{path}\n"
        copy_text = str(path)
        if path.suffix.lower() in {".txt", ".text", ".md", ".markdown", ".rst", ".log"}:
            try:
                text = path.read_text(errors="replace")
            except OSError as exc:
                text = f"(could not read the file: {exc.strerror})"
            body += f"\n{text}"
            copy_text = text
        else:
            body += (
                "\nThis file type cannot be shown in the terminal; open the path "
                "above with the program of your choice (Copy puts it on the "
                "clipboard)."
            )
        self.app.push_screen(
            MessageScreen(path.name, body, markup=False, copy_text=copy_text)
        )


def _shorten_path(path: str, width: int) -> str:
    """Keep the tail of a long path so the vault's own name stays visible."""
    width = max(width, 12)
    if len(path) <= width:
        return path
    return "…" + path[-(width - 1) :]


# The diagnostics are shared with the web app and name its pages; the
# terminal has tabs instead.
_PAGE_NAMES = {
    "Vaults & Documents → Retrieval filter for this vault": "Vault tab (F2) → Retrieval filter",
    "Vaults & Documents → Overwrite": "Vault tab (F2) → Overwrite existing index",
    "Vaults & Documents page": "Vault tab (F2)",
    "Vaults & Documents": "Vault tab (F2)",
    "Settings page": "Settings tab (F6)",
}


def _tui_wording(text: str) -> str:
    """Replace web-page names in shared status text with the tab names."""
    for web, tui in _PAGE_NAMES.items():
        text = text.replace(web, tui)
    return text


_MARKDOWN_PUNCTUATION = set(r"\`*_{}[]()#+-.!|<>~")


def _plain_text_as_markdown(text: str) -> str:
    """Escape ``text`` so a Markdown widget shows it verbatim.

    Backslash-escapes every ASCII punctuation character Markdown could
    interpret and turns each newline into a hard line break, so error
    messages and hints keep their underscores, asterisks, and line layout.
    """
    escaped = "".join(
        f"\\{char}" if char in _MARKDOWN_PUNCTUATION else char for char in text
    )
    return "  \n".join(escaped.split("\n"))


def _set_select(select: Select[str], value: str) -> None:
    """Select ``value`` if it is one of the options, else leave it blank."""
    if value and any(option_value == value for _, option_value in select._options):
        select.value = value
    else:
        select.clear()


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------


class VaultApp(App[None]):
    TITLE = "TalkPipe Vault"
    CSS_PATH = "app.tcss"
    # Nothing is registered with the palette that the tabs do not offer, and its
    # footer entry is what overflows an 80-column terminal.
    ENABLE_COMMAND_PALETTE = False
    # priority=True so the tab keys and Ctrl+Q win over focused widgets
    # (TextArea claims several keys itself).
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f1", "help", "Help", priority=True),
        Binding("f2", "switch_tab('tab-vault')", "Vault", priority=True),
        Binding("f3", "switch_tab('tab-search')", "Search", priority=True),
        Binding("f4", "switch_tab('tab-keywords')", "Keywords", priority=True),
        Binding("f5", "switch_tab('tab-ask')", "Ask", priority=True),
        Binding("f6", "switch_tab('tab-settings')", "Settings", priority=True),
        # Not in the footer: with it, the footer overflows 80 columns. F1 lists it.
        Binding("ctrl+r", "refresh", "Refresh", priority=True, show=False),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(
        self,
        service: VaultService | None = None,
        *,
        vault_path: str = "",
        resume: bool = False,
    ) -> None:
        super().__init__()
        self.service = service or VaultService()
        self._initial_vault = vault_path
        self._resume = resume
        self._index_timer: Any = None
        self._index_message = ""
        self._index_previous_chunks = 0
        self._index_overwrite = False
        self._fulltext_timer: Any = None
        self._pending_confirm: dict[str, Any] | None = None
        # The last answer as the model wrote it — what "Copy answer" copies.
        self._answer_text = ""

    # -- layout ----------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("TalkPipe Vault", id="brand")
            yield Static("no vault open", id="vault-name")
            yield Static("", id="vault-facts")
        with TabbedContent(id="tabs"):
            with TabPane("Vault", id="tab-vault"):
                yield from self._compose_vault_tab()
            with TabPane("Search", id="tab-search"):
                yield from self._compose_search_tab()
            with TabPane("Keywords", id="tab-keywords"):
                yield from self._compose_keywords_tab()
            with TabPane("Ask", id="tab-ask"):
                yield from self._compose_ask_tab()
            with TabPane("Settings", id="tab-settings"):
                yield from self._compose_settings_tab()
        yield Footer(show_command_palette=False)

    def _compose_vault_tab(self) -> ComposeResult:
        # A scroll container: on a 16-row terminal the recent-vaults list and
        # its buttons would otherwise sit below the last row, focusable but
        # invisible.
        with VerticalScroll(id="vault-scroll"):
            yield from self._compose_vault_form()

    def _compose_vault_form(self) -> ComposeResult:
        yield Label(
            "Documents to index (folder or glob pattern)", classes="field-label"
        )
        with Horizontal(classes="form-row"):
            yield Input(placeholder="e.g. ~/Documents/notes", id="source-path")
            yield Button("Browse", id="source-browse")
        yield Label("Vault (index folder; created if missing)", classes="field-label")
        with Horizontal(classes="form-row"):
            yield Input(placeholder=self.service.vault_example(), id="vault-path")
            yield Button("Browse", id="vault-browse")
        with Horizontal(classes="button-row"):
            yield Button("Index documents", variant="primary", id="index")
            yield Button("Open vault", id="open-vault")
            yield Checkbox("Overwrite existing index", False, id="overwrite")
        yield Static("", id="index-progress", markup=False)
        yield Label(
            "Recent vaults (Enter: open · Delete button removes files)",
            classes="field-label",
        )
        yield OptionList(id="recent-vaults")
        with Horizontal(classes="button-row"):
            yield Button("Open selected", id="open-recent")
            yield Button("Delete selected", variant="error", id="delete-recent")
            yield Button("Retrieval filter", id="filter")

    def _compose_search_tab(self) -> ComposeResult:
        with Horizontal(classes="form-row"):
            yield Input(
                placeholder="Search your vault. Works best with a full question...",
                id="search-query",
            )
            yield Button("Search", variant="primary", id="search-go")
        with Horizontal(classes="button-row"):
            yield Checkbox(
                "Apply retrieval filter", False, id="search-filter", classes="hidden"
            )
            yield Button("Copy All", id="search-copy-all")
            yield Static("", id="search-note", classes="status-line")
        yield ResultsPane(
            id="search-results",
            empty_text=(
                "Enter a query above (F3 focuses this tab). Semantic search ranks "
                "by meaning; for an exact word use Keywords (F4)."
            ),
        )

    def _compose_keywords_tab(self) -> ComposeResult:
        with Horizontal(classes="form-row"):
            yield Input(
                placeholder="Enter keywords (e.g., python AND tutorial)...",
                id="kw-query",
            )
            yield Button("Search", variant="primary", id="kw-go")
        with Horizontal(classes="button-row"):
            yield Checkbox(
                "Apply retrieval filter", False, id="kw-filter", classes="hidden"
            )
            yield Button("Copy All", id="kw-copy-all")
            yield Button("Build full-text index", id="kw-build")
            yield Static("", id="kw-note", classes="status-line")
        yield ResultsPane(
            id="kw-results",
            empty_text="Keyword search needs a full-text index; build one with the button above.",
        )

    def _compose_ask_tab(self) -> ComposeResult:
        yield QuestionArea(id="question", soft_wrap=True, tab_behavior="focus")
        with Horizontal(classes="button-row"):
            yield Button("Ask", variant="primary", id="ask-go")
            yield Checkbox(
                "Boost with keyword search", False, id="ask-keyword", classes="hidden"
            )
            yield Static(
                "Keyword boost: build a full-text index on Keywords (F4)",
                id="ask-keyword-hint",
                classes="muted",
            )
            yield Button("Copy answer", id="ask-copy")
        with VerticalScroll(id="answer-pane"):
            yield Static("", id="answer-meta")
            # Answers are LLM output and usually Markdown (tables, headings,
            # emphasis); the Markdown widget lays them out instead of showing
            # the raw pipes and asterisks.
            yield Markdown(
                _plain_text_as_markdown(
                    "Ask a focused question, like: What did I write about "
                    "deployment?\nType it above and press Ask (or Enter)."
                ),
                id="answer",
                open_links=False,
            )
        yield Label("Source chunks", classes="field-label")
        yield ResultsPane(
            id="citations", empty_text="The chunks the answer was based on appear here."
        )

    def _compose_settings_tab(self) -> ComposeResult:
        with VerticalScroll(id="settings-scroll"):
            yield Label("Configuration status", classes="section-heading")
            yield Static("Not checked yet.", id="config-status", markup=False)
            with Horizontal(classes="button-row"):
                yield Button(
                    "Re-test (may download the embedding model)", id="config-retest"
                )
            yield Label("Model settings", classes="section-heading")
            with Horizontal(classes="form-row"):
                with Vertical(classes="form-col"):
                    yield Label("Embedding source", classes="field-label")
                    yield Select([], id="embedding-source", allow_blank=True)
                with Vertical(classes="form-col"):
                    yield Label("Embedding model", classes="field-label")
                    yield Input(id="embedding-model")
            with Horizontal(classes="form-row"):
                with Vertical(classes="form-col"):
                    yield Label("Chat source", classes="field-label")
                    yield Select([], id="chat-source", allow_blank=True)
                with Vertical(classes="form-col"):
                    yield Label("Chat model", classes="field-label")
                    yield Input(id="chat-model")
            with Horizontal(classes="form-row"):
                with Vertical(classes="form-col"):
                    yield Label("Chunk size", classes="field-label")
                    yield Input(id="chunk-size", type="integer", valid_empty=True)
                with Vertical(classes="form-col"):
                    yield Label("Shingle size", classes="field-label")
                    yield Input(id="shingle-size", type="integer", valid_empty=True)
                with Vertical(classes="form-col"):
                    yield Label("Shingle overlap", classes="field-label")
                    yield Input(id="shingle-overlap", type="integer", valid_empty=True)
                with Vertical(classes="form-col"):
                    yield Label("Ask result count", classes="field-label")
                    yield Input(id="rag-result-limit", type="integer", valid_empty=True)
            with Horizontal(classes="button-row"):
                yield Button("Save settings", variant="primary", id="settings-save")
            yield Label("Connections & credentials", classes="section-heading")
            yield Static("", id="credentials-note", classes="muted", markup=False)
            for cred in self.service.settings_view()["credentials"]:
                key = str(cred["key"])
                yield Label(
                    str(cred["label"]), id=f"cred-label-{key}", classes="field-label"
                )
                yield Input(
                    placeholder=str(cred["env_var"]),
                    password=bool(cred["secret"]),
                    id=f"cred-{key}",
                )
                if cred["secret"]:
                    yield Checkbox("Clear the saved key", False, id=f"clear-{key}")
            with Horizontal(classes="button-row"):
                yield Button(
                    "Save connection settings", variant="primary", id="credentials-save"
                )

    # -- lifecycle -----------------------------------------------------------------------

    def on_mount(self) -> None:
        self._apply_size(self.size.height)
        self.query_one("#config-status", Static).update("Checking…")
        self.notify("Opening the vault…", timeout=3)
        self._startup()

    def on_resize(self, event: Any) -> None:
        self._apply_size(event.size.height)

    def _apply_size(self, rows: int) -> None:
        self.screen.set_class(rows < COMPACT_ROWS, "compact")

    @work(thread=True, exclusive=True, group="startup")
    def _startup(self) -> None:
        outcome = self.service.startup(self._initial_vault, resume=self._resume)
        self.call_from_thread(self._after_startup, outcome)

    def _after_startup(self, outcome: dict[str, Any]) -> None:
        if outcome["ok"]:
            if outcome.get("vault_path"):
                self.notify(outcome["message"])
            else:
                self.query_one("#tabs", TabbedContent).active = "tab-vault"
                if self._resume:
                    self.query_one("#index-progress", Static).update(
                        "No recently used vault to resume — open or create one below."
                    )
        else:
            self.notify(outcome["error"], severity="error", timeout=15)
            self.query_one("#tabs", TabbedContent).active = "tab-vault"
        self.refresh_status()
        self._load_recent_vaults()
        self._load_settings_form()
        self._load_config_status(probe=True, download=False)
        if self.service.vault_path:
            self.query_one("#tabs", TabbedContent).active = "tab-search"
            self.query_one("#search-query", Input).focus()
        elif not outcome["ok"] and self._initial_vault:
            # The vault named on the command line could not be opened: keep the
            # path in the form and the reason on screen (toasts expire).
            self.query_one("#vault-path", Input).value = self._initial_vault
            self.query_one("#index-progress", Static).update(outcome["error"])
            self.query_one("#vault-path", Input).focus()
        else:
            self.query_one("#source-path", Input).focus()

    # -- status --------------------------------------------------------------------------

    def refresh_status(self) -> None:
        status = self.service.status()
        name = status["vault_path"] or "no vault open"
        self.query_one("#vault-name", Static).update(
            _shorten_path(name, self.size.width // 2)
        )
        has_vault = bool(status["vault_path"])
        facts = []
        if status["vault_path"]:
            facts.append(f"{status['chunks']} chunks")
            if status["keyword_index_stale"]:
                facts.append("keywords out of date")
            else:
                facts.append(
                    "keywords on"
                    if status["keyword_search_enabled"]
                    else "keywords off"
                )
            if status["filter_active"]:
                facts.append("filter on")
        self.query_one("#vault-facts", Static).update(" · ".join(facts))
        filter_on = bool(status["filter_active"])
        self.query_one("#search-filter", Checkbox).set_class(not filter_on, "hidden")
        self.query_one("#kw-filter", Checkbox).set_class(not filter_on, "hidden")
        self.query_one("#ask-keyword", Checkbox).set_class(
            not status["keyword_search_enabled"], "hidden"
        )
        self.query_one("#ask-keyword-hint", Static).set_class(
            status["keyword_search_enabled"] or not has_vault, "hidden"
        )
        self.query_one("#filter", Button).disabled = not has_vault
        self.query_one("#kw-build", Button).disabled = not has_vault
        self.query_one("#kw-build", Button).label = (
            "Rebuild full-text index"
            if status["keyword_search_enabled"]
            else "Build full-text index"
        )
        self.query_one("#vault-path", Input).value = status["vault_path"]

    def action_refresh(self) -> None:
        self._refresh_worker()

    @work(thread=True, exclusive=True, group="refresh")
    def _refresh_worker(self, *, report_total: bool = False) -> None:
        outcome = self.service.refresh()
        self.call_from_thread(self._notify_outcome, outcome)
        self.call_from_thread(self.refresh_status)
        if report_total:
            self.call_from_thread(self._report_vault_total)

    def _report_vault_total(self) -> None:
        """Append the vault's chunk count to the indexing summary.

        The summary counts only the chunks this run added; without the total,
        indexing a folder twice (Overwrite unticked) looks like nothing changed
        while every search returns duplicates.
        """
        status = self.service.status()
        chunks = status["chunks"]
        message = f"{self._index_message} The vault now holds {chunks} chunk(s)."
        if status["keyword_index_stale"]:
            message += (
                " The full-text index does not include this run — rebuild it on "
                "the Keywords tab (F4) for keyword search and the Ask boost."
            )
        if not self._index_overwrite and self._index_previous_chunks:
            message += (
                f" Overwrite was off, so the {self._index_previous_chunks} chunk(s) "
                "already in the vault were kept — any file indexed before is now "
                "in it twice; tick Overwrite existing index to replace the index."
            )
        self.query_one("#index-progress", Static).update(message)

    def _notify_outcome(self, outcome: dict[str, Any]) -> None:
        if outcome["ok"]:
            if outcome["message"]:
                self.notify(outcome["message"])
        else:
            self.notify(outcome["error"], severity="error", timeout=10)

    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab_id
        focus_targets = {
            "tab-vault": "#source-path",
            "tab-search": "#search-query",
            "tab-keywords": "#kw-query",
            "tab-ask": "#question",
            "tab-settings": "#config-retest",
        }
        target = focus_targets.get(tab_id)
        if target:
            self.query_one(target).focus()

    def action_help(self) -> None:
        self.push_screen(MessageScreen("Help", HELP_TEXT))

    # -- vault tab -----------------------------------------------------------------------

    def _load_recent_vaults(self) -> None:
        options = self.query_one("#recent-vaults", OptionList)
        options.clear_options()
        for row in self.service.recent_vaults():
            marks = []
            if row["open"]:
                marks.append("open")
            if not row["exists"]:
                marks.append("missing")
            suffix = f"  ({', '.join(marks)})" if marks else ""
            options.add_option(Option(f"{row['path']}{suffix}", id=row["path"]))

    def _selected_recent(self) -> str | None:
        options = self.query_one("#recent-vaults", OptionList)
        index = options.highlighted
        if index is None:
            return None
        option_id = options.get_option_at_index(index).id
        return str(option_id) if option_id else None

    @on(Button.Pressed, "#source-browse")
    @work(group="modal")
    async def _browse_source(self) -> None:
        start = self.query_one("#source-path", Input).value.strip()
        chosen = await self.push_screen_wait(DirectoryPickerScreen(self.service, start))
        if chosen:
            self.query_one("#source-path", Input).value = chosen
            self._suggest_vault(chosen)

    @on(Button.Pressed, "#vault-browse")
    @work(group="modal")
    async def _browse_vault(self) -> None:
        start = self.query_one("#vault-path", Input).value.strip()
        chosen = await self.push_screen_wait(DirectoryPickerScreen(self.service, start))
        if chosen:
            self.query_one("#vault-path", Input).value = chosen

    @on(Input.Changed, "#source-path")
    def _source_changed(self, event: Input.Changed) -> None:
        if (
            not self.service.vault_path
            and not self.query_one("#vault-path", Input).value
        ):
            self._suggest_vault(event.value)

    def _suggest_vault(self, source: str) -> None:
        if self.service.vault_path:
            return
        suggestion = self.service.suggest_vault_path(source)
        if suggestion:
            self.query_one("#vault-path", Input).value = suggestion

    @on(Input.Submitted, "#source-path")
    @on(Input.Submitted, "#vault-path")
    @on(Button.Pressed, "#index")
    def _index_pressed(self) -> None:
        source = self.query_one("#source-path", Input).value.strip()
        vault = self.query_one("#vault-path", Input).value.strip()
        overwrite = self.query_one("#overwrite", Checkbox).value
        if not source:
            if vault:
                self._open_vault(vault)
            else:
                self.notify(
                    "Enter a folder or glob pattern to index.", severity="warning"
                )
            return
        status = self.service.status()
        self._index_previous_chunks = (
            status["chunks"] if status["vault_path"] == vault else 0
        )
        self._start_indexing(source, vault, overwrite, confirm=False)

    @work(thread=True, exclusive=True, group="index-start")
    def _start_indexing(
        self, source: str, vault: str, overwrite: bool, *, confirm: bool
    ) -> None:
        outcome = self.service.start_indexing(
            source, vault, overwrite=overwrite, confirm_non_vault=confirm
        )
        self.call_from_thread(
            self._after_index_start, outcome, source, vault, overwrite
        )

    def _after_index_start(
        self, outcome: dict[str, Any], source: str, vault: str, overwrite: bool
    ) -> None:
        if outcome.get("needs_confirm"):
            self._confirm_non_vault(
                outcome,
                lambda: self._start_indexing(source, vault, overwrite, confirm=True),
            )
            return
        self._notify_outcome(outcome)
        self.refresh_status()
        self._load_recent_vaults()
        progress = self.query_one("#index-progress", Static)
        if outcome["ok"]:
            self._index_overwrite = overwrite
            progress.update("Indexing started…")
            self._index_timer = self.set_interval(1.0, self._poll_index)
        else:
            # Toasts expire; a refusal (path fence, unreadable folder) stays
            # readable in the form.
            progress.update(outcome["error"])

    @work(group="modal")
    async def _confirm_non_vault(
        self, outcome: dict[str, Any], proceed: Callable[[], Any]
    ) -> None:
        confirmed = await self.push_screen_wait(
            ConfirmScreen(
                "This folder already contains files",
                f"{outcome['confirm_path']} holds {outcome['entry_count']} item(s) "
                "that are not vault data. Creating a vault here writes index files "
                "alongside them, and deleting the vault later removes the whole "
                "folder. If you meant to search those documents, keep the vault "
                "elsewhere and index this folder instead.",
                confirm_label="Create the vault here anyway",
            )
        )
        if confirmed:
            proceed()

    def _poll_index(self) -> None:
        snap = self.service.index_status()
        progress = self.query_one("#index-progress", Static)
        if snap["running"]:
            if snap["phase"] == "counting":
                progress.update(f"Counting files in {snap['source']}…")
            else:
                current = (
                    Path(snap["current_file"]).name if snap["current_file"] else ""
                )
                progress.update(
                    f"Indexing {snap['files_done']}/{snap['total_files']} files, "
                    f"{snap['chunks']} chunks · {current}"
                )
            return
        if self._index_timer is not None:
            self._index_timer.stop()
            self._index_timer = None
        if snap["error"]:
            progress.update(f"Indexing failed: {snap['error']}")
            self.notify(snap["error"], severity="error", timeout=15)
        else:
            message = snap["message"] or "Indexing finished."
            progress.update(message)
            self.notify(message)
            self._index_message = message
            # A replace run is a one-off: left ticked, the next "add these
            # documents" run would silently wipe the vault (the web form
            # comes back unticked as well).
            self.query_one("#overwrite", Checkbox).value = False
        self._refresh_worker(report_total=snap["error"] is None)

    @on(Button.Pressed, "#open-vault")
    def _open_vault_pressed(self) -> None:
        vault = self.query_one("#vault-path", Input).value.strip()
        if not vault:
            self.notify("Enter a vault path.", severity="warning")
            return
        self._open_vault(vault)

    @on(OptionList.OptionSelected, "#recent-vaults")
    def _recent_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self._open_vault(str(event.option.id))

    @on(Button.Pressed, "#open-recent")
    def _open_recent(self) -> None:
        path = self._selected_recent()
        if path:
            self._open_vault(path)
        else:
            self.notify("Select a recent vault first.", severity="warning")

    def _open_vault(self, path: str, *, confirm: bool = False) -> None:
        self.notify(f"Opening {path}…", timeout=3)
        self._open_vault_worker(path, confirm)

    @work(thread=True, exclusive=True, group="open-vault")
    def _open_vault_worker(self, path: str, confirm: bool) -> None:
        outcome = self.service.open_vault(path, confirm_non_vault=confirm)
        self.call_from_thread(self._after_open_vault, outcome, path)

    def _after_open_vault(self, outcome: dict[str, Any], path: str) -> None:
        if outcome.get("needs_confirm"):
            self._confirm_non_vault(
                outcome, lambda: self._open_vault(path, confirm=True)
            )
            return
        self._notify_outcome(outcome)
        self.refresh_status()
        self._load_recent_vaults()
        self._load_config_status(probe=False, download=False)
        if not outcome["ok"]:
            self.query_one("#index-progress", Static).update(outcome["error"])
        if outcome["ok"] and not outcome.get("created"):
            self.query_one("#tabs", TabbedContent).active = "tab-search"
            self.query_one("#search-query", Input).focus()

    @on(Button.Pressed, "#delete-recent")
    @work(group="modal")
    async def _delete_recent(self) -> None:
        path = self._selected_recent()
        if not path:
            self.notify("Select a recent vault first.", severity="warning")
            return
        if any(
            row["open"] for row in self.service.recent_vaults() if row["path"] == path
        ):
            # Refuse up front rather than after the "cannot be undone" dialog.
            self.notify(
                "Cannot delete the vault that is currently open. Open a different "
                "vault first.",
                severity="error",
            )
            return
        confirmed = await self.push_screen_wait(
            ConfirmScreen(
                "Delete vault",
                f"Delete the vault folder {path} and everything in it (index files, "
                "full-text index, retrieval filter)? The indexed documents "
                "themselves are not touched. This cannot be undone.",
                confirm_label="Delete",
            )
        )
        if not confirmed:
            return
        outcome = self.service.delete_vault(path)
        self._notify_outcome(outcome)
        self._load_recent_vaults()

    @on(Button.Pressed, "#filter")
    @work(group="modal")
    async def _edit_filter(self) -> None:
        if not self.service.vault_path:
            self.notify("Open a vault first.", severity="warning")
            return
        values = await self.push_screen_wait(
            RetrievalFilterScreen(self.service.filter_view())
        )
        if values is None:
            return
        outcome = self.service.save_filter(**values)
        self._notify_outcome(outcome)
        self.refresh_status()

    # -- search tabs ----------------------------------------------------------------------

    @on(Input.Submitted, "#search-query")
    @on(Button.Pressed, "#search-go")
    def _search_pressed(self) -> None:
        text = self.query_one("#search-query", Input).value.strip()
        if not text:
            self.query_one("#search-note", Static).update("Enter a query first.")
            return
        self.query_one("#search-note", Static).update("Searching…")
        self._search_worker(text, self.query_one("#search-filter", Checkbox).value)

    @work(thread=True, exclusive=True, group="search")
    def _search_worker(self, text: str, apply_filter: bool) -> None:
        outcome = self.service.semantic_search(text, apply_filter=apply_filter)
        self.call_from_thread(self._show_search, "search", outcome, text)

    @on(Input.Submitted, "#kw-query")
    @on(Button.Pressed, "#kw-go")
    def _kw_pressed(self) -> None:
        text = self.query_one("#kw-query", Input).value.strip()
        if not text:
            self.query_one("#kw-note", Static).update("Enter a query first.")
            return
        self.query_one("#kw-note", Static).update("Searching…")
        self._kw_worker(text, self.query_one("#kw-filter", Checkbox).value)

    @work(thread=True, exclusive=True, group="kw-search")
    def _kw_worker(self, text: str, apply_filter: bool) -> None:
        outcome = self.service.keyword_search(text, apply_filter=apply_filter)
        self.call_from_thread(self._show_search, "kw", outcome, text)

    def _show_search(self, prefix: str, outcome: dict[str, Any], text: str) -> None:
        note = self.query_one(f"#{prefix}-note", Static)
        pane = self.query_one(f"#{prefix}-results", ResultsPane)
        self.refresh_status()
        if not outcome["ok"]:
            note.update(f"Error: {outcome['error']}")
            note.set_classes("status-line error")
            pane.show_results([], empty_text=outcome["error"])
            return
        results = outcome["results"]
        note.set_classes("status-line")
        summary = f"{len(results)} result{'s' if len(results) != 1 else ''}"
        if outcome.get("note"):
            summary += f" · {outcome['note']}"
        note.update(summary)
        empty_text = f'No results found for "{text}".'
        if outcome.get("empty_text"):
            empty_text += f" {outcome['empty_text']}"
        pane.show_results(results, empty_text=empty_text)
        if results:
            pane.option_list.focus()

    @on(Button.Pressed, "#search-copy-all")
    def _search_copy_all(self) -> None:
        self.query_one("#search-results", ResultsPane).copy_all()

    @on(Button.Pressed, "#kw-copy-all")
    def _kw_copy_all(self) -> None:
        self.query_one("#kw-results", ResultsPane).copy_all()

    @on(Button.Pressed, "#kw-build")
    @work(group="modal")
    async def _build_fulltext(self) -> None:
        status = self.service.status()
        if status["keyword_search_enabled"]:
            confirmed = await self.push_screen_wait(
                ConfirmScreen(
                    "Rebuild full-text index",
                    "Rebuild the full-text index from the vault's indexed chunks? "
                    "Keyword search is unavailable while it rebuilds.",
                    confirm_label="Rebuild",
                )
            )
            if not confirmed:
                return
        outcome = self.service.start_fulltext_index()
        self._notify_outcome(outcome)
        if outcome["ok"]:
            self.query_one("#kw-note", Static).update("Building the full-text index…")
            self._fulltext_timer = self.set_interval(1.0, self._poll_fulltext)

    def _poll_fulltext(self) -> None:
        snap = self.service.fulltext_status()
        note = self.query_one("#kw-note", Static)
        if snap["running"]:
            note.update(
                f"Building full-text index: {snap['docs_done']}/{snap['total_docs']} documents"
            )
            return
        if self._fulltext_timer is not None:
            self._fulltext_timer.stop()
            self._fulltext_timer = None
        if snap["error"]:
            note.update(snap["error"])
            note.set_classes("status-line error")
            self.notify(snap["error"], severity="error", timeout=15)
        else:
            note.update(snap["message"] or "Full-text index built.")
            note.set_classes("status-line success")
            self.notify(snap["message"] or "Full-text index built.")
        self._refresh_worker()

    # -- ask -------------------------------------------------------------------------------

    @on(Button.Pressed, "#ask-go")
    def _ask_pressed(self) -> None:
        question = self.query_one("#question", TextArea).text.strip()
        if not question:
            self.notify("Enter a question.", severity="warning")
            return
        self.query_one("#answer-meta", Static).update("Thinking…")
        self._answer_text = ""
        self.query_one("#answer", Markdown).update("")
        self._ask_worker(question, self.query_one("#ask-keyword", Checkbox).value)

    def on_key(self, event: Any) -> None:
        # Enter in the question box asks; Shift+Enter (where the terminal
        # sends it) inserts a newline as usual.
        if (
            event.key == "enter"
            and self.focused is not None
            and self.focused.id == "question"
        ):
            event.prevent_default()
            event.stop()
            self._ask_pressed()

    def ask_current_question(self) -> None:
        """Submit the question box (Enter in the box, or the Ask button)."""
        self._ask_pressed()

    @work(thread=True, exclusive=True, group="ask")
    def _ask_worker(self, question: str, keyword: bool) -> None:
        outcome = self.service.ask(question, use_keyword_search=keyword)
        self.call_from_thread(self._show_answer, outcome)

    def _show_answer(self, outcome: dict[str, Any]) -> None:
        meta = self.query_one("#answer-meta", Static)
        answer = self.query_one("#answer", Markdown)
        citations = self.query_one("#citations", ResultsPane)
        self.refresh_status()
        if not outcome["ok"]:
            meta.update("Error")
            self._answer_text = ""
            # Error text is prose, not Markdown: show it verbatim.
            answer.update(_plain_text_as_markdown(outcome["error"]))
            # The previous question's chunks are not this error's sources.
            citations.show_results([], empty_text="No answer — see the message above.")
            self.notify(outcome["error"], severity="error", timeout=12)
            return
        meta.update(f"Answered by {outcome['answered_by']}")
        self._answer_text = str(outcome["answer"])
        answer.update(self._answer_text)
        citations.show_results(
            outcome["citations"], empty_text="No source chunks were retrieved."
        )

    @on(Button.Pressed, "#ask-copy")
    def _copy_answer(self) -> None:
        # Copy the answer as the model wrote it (Markdown source), not the
        # rendered layout, so it pastes cleanly into notes and editors.
        text = self._answer_text
        if not text.strip():
            self.notify("Nothing to copy yet.", severity="warning")
            return
        self.copy_to_clipboard(text)
        self.notify("Answer copied to the clipboard.")

    # -- settings ---------------------------------------------------------------------------

    def _load_settings_form(self) -> None:
        view = self.service.settings_view()
        overrides = view["overrides"]
        models = view["models"]
        embedding = self.query_one("#embedding-source", Select)
        embedding.set_options([(s, s) for s in view["embedding_sources"]])
        _set_select(
            embedding, overrides["embedding_source"] or models["embedding_source"]
        )
        chat = self.query_one("#chat-source", Select)
        chat.set_options([(s, s) for s in view["chat_sources"]])
        _set_select(chat, overrides["chat_source"] or models["chat_source"])
        self.query_one("#embedding-model", Input).value = overrides["embedding_model"]
        self.query_one(
            "#embedding-model", Input
        ).placeholder = f"default: {models['embedding_model']}"
        self.query_one("#chat-model", Input).value = overrides["chat_model"]
        self.query_one(
            "#chat-model", Input
        ).placeholder = f"default: {models['chat_model']}"
        for key, widget_id in (
            ("chunk_size", "#chunk-size"),
            ("shingle_size", "#shingle-size"),
            ("shingle_overlap", "#shingle-overlap"),
            ("rag_result_limit", "#rag-result-limit"),
        ):
            field = self.query_one(widget_id, Input)
            field.value = "" if overrides[key] is None else str(overrides[key])
            field.placeholder = f"default: {models[key]}"
        for cred in view["credentials"]:
            key = str(cred["key"])
            hint = ""
            if cred["secret"]:
                hint = (
                    f" (saved: {cred['masked']})" if cred["present"] else " (not set)"
                )
            elif cred.get("active") and not cred.get("value"):
                hint = f" (from environment: {cred['active']})"
            self.query_one(f"#cred-label-{key}", Label).update(f"{cred['label']}{hint}")
            field = self.query_one(f"#cred-{key}", Input)
            field.value = "" if cred["secret"] else str(cred.get("value") or "")
            if cred["secret"]:
                self.query_one(f"#clear-{key}", Checkbox).value = False
        self.query_one("#credentials-note", Static).update(
            f"Stored in {view['credentials_store']} and applied to this app only. "
            "A blank secret keeps the saved key; tick Clear to remove it."
        )

    @on(Button.Pressed, "#settings-save")
    def _save_settings(self) -> None:
        def sel(widget_id: str) -> str:
            select = self.query_one(widget_id, Select)
            return "" if select.is_blank() else str(select.value)

        outcome = self.service.save_settings(
            embedding_source=sel("#embedding-source"),
            embedding_model=self.query_one("#embedding-model", Input).value.strip(),
            chat_source=sel("#chat-source"),
            chat_model=self.query_one("#chat-model", Input).value.strip(),
            chunk_size=self.query_one("#chunk-size", Input).value,
            shingle_size=self.query_one("#shingle-size", Input).value,
            shingle_overlap=self.query_one("#shingle-overlap", Input).value,
            rag_result_limit=self.query_one("#rag-result-limit", Input).value,
        )
        self._notify_outcome(outcome)
        if outcome["ok"]:
            self._load_settings_form()
            self.refresh_status()
            self.query_one("#config-status", Static).update("Checking (live probes)…")
            self._load_config_status(probe=True, download=False, announce=True)

    @on(Button.Pressed, "#credentials-save")
    def _save_credentials(self) -> None:
        view = self.service.settings_view()
        changes: dict[str, str | None] = {}
        for cred in view["credentials"]:
            key = str(cred["key"])
            value = self.query_one(f"#cred-{key}", Input).value
            if cred["secret"]:
                if self.query_one(f"#clear-{key}", Checkbox).value:
                    changes[key] = ""
                elif value.strip():
                    changes[key] = value
            else:
                changes[key] = value
        outcome = self.service.save_credentials(changes)
        self._notify_outcome(outcome)
        if outcome["ok"]:
            self._load_settings_form()
            self.query_one("#config-status", Static).update("Checking (live probes)…")
            self._load_config_status(probe=True, download=False, announce=True)

    @on(Button.Pressed, "#config-retest")
    def _retest(self) -> None:
        self.query_one("#config-status", Static).update("Checking (live probes)…")
        self._load_config_status(probe=True, download=True)

    @work(thread=True, exclusive=True, group="config-status")
    def _load_config_status(
        self, *, probe: bool, download: bool, announce: bool = False
    ) -> None:
        report = self.service.config_status(probe=probe, download=download)
        self.call_from_thread(self._show_config_status, report, probe, announce)

    def _show_config_status(
        self, report: dict[str, Any], probed: bool, announce: bool = False
    ) -> None:
        marks = {"ok": "OK ", "warn": "WARN", "error": "FAIL"}
        overall = str(report.get("overall", "")).upper()
        lines = [f"Overall: {overall}" + ("" if probed else " (not probed)")]
        for check in report.get("checks", []):
            status = marks.get(str(check.get("status")), str(check.get("status")))
            value = f" — {check['value']}" if check.get("value") else ""
            lines.append(f"[{status}] {check.get('name', '')}{value}")
            if check.get("summary"):
                lines.append(f"       {_tui_wording(str(check['summary']))}")
            if check.get("fix"):
                lines.append(f"       Fix: {_tui_wording(str(check['fix']))}")
            if check.get("detail"):
                # The probe's own words (timeout, cache path, exception): the
                # only way to tell a slow load from a broken model.
                lines.append(f"       Detail: {check['detail']}")
        self.query_one("#config-status", Static).update("\n".join(lines))
        if announce and overall and overall != "OK":
            # After Save the status panel is usually scrolled off the top of
            # the tab; say where the problem is described.
            self.notify(
                f"Configuration status: {overall} — see the top of the Settings "
                "tab (Shift+Tab to scroll up).",
                severity="warning",
                timeout=8,
            )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def prepare_process_for_textual() -> None:
    """Do the one-time process setup that must not happen under Textual.

    The first tqdm progress bar in a process creates a multiprocessing lock,
    which registers with multiprocessing's resource tracker, which spawns a
    helper process. model2vec wraps its embedding batches in tqdm, so the
    first embedding (the Settings probe, a search, or an indexing run) would
    trigger that spawn from a worker thread while Textual owns the terminal —
    and on Python 3.14 that fails with "bad value(s) in fds_to_keep", making
    a perfectly good cached model report as broken. Creating the lock here,
    before the app starts, moves the spawn to a normal context.
    """
    try:
        from tqdm import tqdm
    except ImportError:  # pragma: no cover - tqdm ships with model2vec
        return
    try:
        tqdm.get_lock()
    except Exception:  # pragma: no cover - never let setup abort the app
        logging.getLogger(__name__).debug("tqdm lock setup failed", exc_info=True)


def main(argv: list[str] | None = None) -> None:
    """Console entry point: ``vault-tui``."""
    from talkpipe.util.config import configure_logger

    from talkpipe_vault import memtune

    memtune.limit_malloc_arenas()
    parser = argparse.ArgumentParser(
        prog="vault-tui",
        description=(
            "Terminal interface for TalkPipe Vault: the web interface's features "
            "(vaults, indexing, semantic and keyword search, Ask, settings) in a "
            "terminal. Runs in-process — no vault-server needed."
        ),
        epilog=(
            "Recent vaults, model settings and credentials are read from and "
            "written to the same files as the web interface "
            "(TALKPIPE_VAULT_HOME, default ~/.talkpipe-vault). Press F1 inside "
            "the application for the keyboard reference."
        ),
    )
    parser.add_argument(
        "vault_path",
        nargs="?",
        default="",
        help="Vault folder to open (created if missing). Omit to choose one in the app.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Open the most recently used vault (falls back to vault_path).",
    )
    parser.add_argument(
        "--show-source-paths",
        action="store_true",
        help="Show source file paths in results (hidden by default).",
    )
    args = parser.parse_args(argv)
    configure_logger("root:ERROR")
    prepare_process_for_textual()
    service = VaultService(show_source_paths=args.show_source_paths)
    app = VaultApp(service, vault_path=args.vault_path, resume=args.resume)
    try:
        app.run()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
