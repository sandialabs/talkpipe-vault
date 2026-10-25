"""Tests for the folderChanges source (pipelines/folder_changes.py)."""

import os
import queue
import threading
import time

import pytest
from talkpipe.chatterlang.compiler import compile

from talkpipe_vault.pipelines import folder_changes as fc
from talkpipe_vault.pipelines import folder_index
from talkpipe_vault.pipelines.config import FOLDER_INDEX_DIRNAME
from tests.conftest import FAKE_EMBEDDING_SOURCE


@pytest.fixture
def vault_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("TALKPIPE_VAULT_HOME", str(home))
    monkeypatch.delenv("TALKPIPE_VAULT_ROOT", raising=False)
    return home


@pytest.fixture
def docs(tmp_path, vault_home):
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    (root / "alpha.txt").write_text("The lighthouse keeper logged a storm.\n")
    (root / "sub" / "bravo.md").write_text("Garden journal: basil and tomatoes.\n")
    (root / "image.png").write_bytes(b"\x89PNG not a document")
    (root / ".git").mkdir()
    (root / ".git" / "notes.txt").write_text("hidden\n")
    (root / ".secret.md").write_text("hidden\n")
    (root / FOLDER_INDEX_DIRNAME).mkdir()
    (root / FOLDER_INDEX_DIRNAME / "status.txt").write_text("an index file\n")
    return root


def _pass(root, **kwargs):
    """One pass over the folder, as (event, relpath) pairs."""
    items = fc.folder_changes(path=str(root), watch=False, **kwargs).generate()
    return [(item["event"], item["relpath"]) for item in items]


def test_first_pass_reports_every_document(docs):
    [first, second] = fc.folder_changes(path=str(docs), watch=False).generate()
    assert first == {
        "event": "added",
        "path": str(docs.resolve() / "alpha.txt"),
        "relpath": "alpha.txt",
        "folder": str(docs.resolve()),
    }
    assert second["relpath"] == "sub/bravo.md"
    state = docs / fc.STREAM_STATE_DIRNAME
    assert (state / "default.db").is_file()
    assert (state / ".gitignore").read_text() == "*\n"
    assert not (state / "CACHEDIR.TAG").exists()


def test_a_restart_reports_only_what_changed(docs):
    _pass(docs)
    (docs / "alpha.txt").write_text("The lighthouse lamp failed.\n")
    later = time.time() + 5
    os.utime(docs / "sub" / "bravo.md", (later, later))  # touched, not changed
    (docs / "sub" / "charlie.md").write_text("New notes.\n")
    assert _pass(docs) == [("changed", "alpha.txt"), ("added", "sub/charlie.md")]
    (docs / "sub" / "bravo.md").unlink()
    assert _pass(docs) == [("deleted", "sub/bravo.md")]
    assert _pass(docs) == []


def test_a_rename_is_a_delete_then_an_add(docs):
    _pass(docs)
    (docs / "sub").rename(docs / "garden")
    assert _pass(docs) == [("deleted", "sub/bravo.md"), ("added", "garden/bravo.md")]


def test_each_name_keeps_its_own_record(docs):
    _pass(docs)
    assert _pass(docs) == []
    assert len(_pass(docs, name="summaries")) == 2
    assert (docs / fc.STREAM_STATE_DIRNAME / "summaries.db").is_file()


def test_an_explicit_state_file(docs, tmp_path):
    state = tmp_path / "elsewhere" / "stream.db"
    state.parent.mkdir()
    assert len(_pass(docs, state=str(state))) == 2
    assert _pass(docs, state=str(state)) == []
    assert not (docs / fc.STREAM_STATE_DIRNAME).exists()


def test_a_name_cannot_leave_the_state_folder(docs):
    with pytest.raises(ValueError):
        _pass(docs, name="../escape")


def test_a_second_reader_of_the_same_record_is_refused(docs):
    held = fc.FolderChanges(docs, fc.stream_state_path(docs))
    held.open(watch=False)
    try:
        with pytest.raises(RuntimeError, match="different name"):
            _pass(docs)
    finally:
        held.close()


def test_a_synced_folder_keeps_its_record_in_app_data(tmp_path, vault_home):
    synced = tmp_path / "Dropbox" / "notes"
    synced.mkdir(parents=True)
    state = fc.stream_state_path(synced)
    assert state.is_relative_to(vault_home / fc.EXTERNAL_STREAM_DIRNAME)
    assert not (synced / fc.STREAM_STATE_DIRNAME).exists()


def test_an_item_being_handled_when_stopped_comes_again(docs):
    items = fc.folder_changes(path=str(docs), watch=False).generate()
    assert next(items)["relpath"] == "alpha.txt"
    items.close()  # stopped before asking for the next item
    assert _pass(docs) == [("added", "alpha.txt"), ("added", "sub/bravo.md")]


def test_max_events_records_the_last_item(docs):
    assert _pass(docs, max_events=1) == [("added", "alpha.txt")]
    assert _pass(docs) == [("added", "sub/bravo.md")]


def test_ctrl_c_ends_the_stream_cleanly(docs, monkeypatch):
    """Ctrl+C is the documented way to stop: no traceback, the stream just ends."""
    real_sleep = time.sleep

    def interrupted_sleep(seconds):
        if threading.current_thread() is threading.main_thread():
            raise KeyboardInterrupt
        real_sleep(seconds)

    monkeypatch.setattr(fc.time, "sleep", interrupted_sleep)
    events = [c["event"] for c in fc.folder_changes(path=str(docs))()]
    assert events == ["added", "added"]


def _next_within(items, timeout=30.0):
    """The next item, failing instead of hanging when none comes."""
    box: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: box.put(next(items)), daemon=True).start()
    try:
        return box.get(timeout=timeout)
    except queue.Empty:
        pytest.fail(f"no item within {timeout} s")


def test_watching_reports_changes_as_they_happen(docs):
    items = fc.folder_changes(path=str(docs)).generate()
    try:
        assert _next_within(items)["relpath"] == "alpha.txt"
        assert _next_within(items)["relpath"] == "sub/bravo.md"
        (docs / "sub" / "delta.md").write_text("Written while watching.\n")
        item = _next_within(items)
        assert (item["event"], item["relpath"]) == ("added", "sub/delta.md")
        (docs / "alpha.txt").unlink()
        item = _next_within(items)
        assert (item["event"], item["relpath"]) == ("deleted", "alpha.txt")
    finally:
        items.close()


def test_chatterlang_pipeline_reads_the_documents(docs):
    _pass(docs)
    (docs / "alpha.txt").write_text("The lighthouse lamp failed.\n")
    (docs / "sub" / "bravo.md").unlink()
    script = (
        f'INPUT FROM folderChanges[path="{docs}", watch=False] '
        "| lambdaFilter[expression=\"event != 'deleted'\"] "
        '| readFile[field="path", set_as="extracted"] '
        '| toDict[field_list="event,relpath,extracted.content:content"]'
    )
    assert list(compile(script)()) == [
        {
            "event": "changed",
            "relpath": "alpha.txt",
            "content": "The lighthouse lamp failed.\n",
        }
    ]
    # The delete was filtered out downstream, but this stream was sent it.
    assert _pass(docs) == []


def test_the_folder_index_and_the_stream_ignore_each_other(docs, fake_embedder):
    assert {rel for _, rel in _pass(docs)} == {"alpha.txt", "sub/bravo.md"}
    (docs / fc.STREAM_STATE_DIRNAME / "notes.txt").write_text("stream state\n")
    folder = folder_index.open_folder(
        docs, embedding_source=FAKE_EMBEDDING_SOURCE, embedding_model="fake"
    )
    status = folder_index.index_folder_now(folder, vectors=False)
    assert status["files_total"] == 2
    assert _pass(docs) == []
