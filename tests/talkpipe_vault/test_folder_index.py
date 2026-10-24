"""Tests for folder mode's indexing engine (pipelines/folder_index.py)."""

import multiprocessing
import os
import signal
import stat
import time
from pathlib import Path

import pytest
from talkpipe.search.lancedb import LanceDBDocumentStore
from talkpipe.search.whoosh import WhooshFullTextIndex

from talkpipe_vault.pipelines import folder_index, vault_metadata
from talkpipe_vault.pipelines.config import (
    DEFAULT_VECTOR_TABLE_NAME,
    FOLDER_INDEX_DIRNAME,
    get_whoosh_index_path,
)
from tests.conftest import FAKE_EMBEDDING_SOURCE

WORDS = [
    "river",
    "mountain",
    "lantern",
    "harvest",
    "copper",
    "meadow",
    "signal",
    "orchard",
    "timber",
    "compass",
    "beacon",
    "glacier",
]


def _text(seed: str, n: int = 120) -> str:
    """A few hundred characters of text that chunks into several shingles."""
    words = [seed] + [WORDS[(i * 7 + len(seed)) % len(WORDS)] for i in range(n)]
    return " ".join(words) + ".\n"


@pytest.fixture
def vault_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("TALKPIPE_VAULT_HOME", str(home))
    monkeypatch.delenv("TALKPIPE_VAULT_ROOT", raising=False)
    return home


@pytest.fixture
def docs(tmp_path):
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    (root / "alpha.txt").write_text(_text("alpha"))
    (root / "sub" / "bravo.md").write_text(_text("bravo"))
    (root / "image.png").write_bytes(b"\x89PNG not a document")
    return root


def _open(root, **kwargs):
    return folder_index.open_folder(
        root,
        embedding_source=FAKE_EMBEDDING_SOURCE,
        embedding_model="fake",
        **kwargs,
    )


def _whoosh_ids(folder):
    path = get_whoosh_index_path(folder.index_path)
    if not os.path.isdir(path):
        return set()
    with WhooshFullTextIndex(path) as ix, ix.ix.searcher() as searcher:
        return {fields["doc_id"] for fields in searcher.all_stored_fields()}


def _lance_ids(folder):
    store = LanceDBDocumentStore(
        path=folder.index_path, table_name=DEFAULT_VECTOR_TABLE_NAME
    )
    try:
        return set(store.list_ids())
    except Exception:
        return set()


def _ids_for(ids, folder, relpath):
    prefix = folder._file_prefix(folder_index._file_key(relpath))
    return {i for i in ids if i.startswith(prefix)}


# -- where the index lives ----------------------------------------------------


def test_writable_folder_keeps_its_index_inside(docs, vault_home, fake_embedder):
    folder = _open(docs)
    assert folder.location.in_folder
    assert Path(folder.index_path) == docs.resolve() / FOLDER_INDEX_DIRNAME
    assert folder.try_become_writer(watch=False)
    try:
        index = Path(folder.index_path)
        assert (index / ".gitignore").read_text() == "*\n"
        assert (
            (index / "CACHEDIR.TAG")
            .read_text()
            .startswith("Signature: 8a477f597d28d172789f06886806bc55")
        )
    finally:
        folder.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_read_only_folder_indexes_into_app_data(docs, vault_home):
    docs.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        location = folder_index.resolve_index_location(docs, vault_home)
    finally:
        docs.chmod(stat.S_IRWXU)
    assert not location.in_folder
    assert location.path.parent == vault_home / folder_index.EXTERNAL_INDEX_DIRNAME
    assert "not writable" in location.reason
    assert not (docs / FOLDER_INDEX_DIRNAME).exists()


def test_vault_root_forces_app_data(docs, tmp_path, monkeypatch, fake_embedder):
    root = tmp_path / "vault-root"
    root.mkdir()
    monkeypatch.setenv("TALKPIPE_VAULT_ROOT", str(root))
    folder = _open(docs)
    assert not folder.location.in_folder
    assert Path(folder.index_path).is_relative_to(root.resolve())


def test_sync_folder_indexes_into_app_data(tmp_path, vault_home):
    synced = tmp_path / "Dropbox" / "notes"
    synced.mkdir(parents=True)
    location = folder_index.resolve_index_location(synced, vault_home)
    assert not location.in_folder
    assert "synced" in location.reason


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_existing_read_only_index_opens_as_reader(docs, vault_home, fake_embedder):
    folder = _open(docs)
    folder_index.index_folder_now(folder, vectors=False)
    index = docs / FOLDER_INDEX_DIRNAME
    index.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        reopened = _open(docs)
        assert reopened.location.read_only
        assert not reopened.try_become_writer(watch=False)
        assert reopened.status()["role"] == "read-only"
    finally:
        index.chmod(stat.S_IRWXU)


# -- indexing -------------------------------------------------------------------


def test_keywords_are_searchable_before_any_vector(docs, vault_home, fake_embedder):
    folder = _open(docs)
    assert folder.try_become_writer(watch=False)
    try:
        folder.scan()
        for _ in range(10):
            if not folder.has_keyword_work():
                break
            folder.keyword_batch()
        status = folder.status()
        assert status["files_total"] == 2  # the .png has no extractor
        assert status["files_keyword"] == 2
        assert status["files_embedded"] == 0
        assert status["phase"] == "meaning"
        assert _whoosh_ids(folder)
        assert not _lance_ids(folder)
        with WhooshFullTextIndex(get_whoosh_index_path(folder.index_path)) as ix:
            hits = ix.text_search("bravo")
        assert hits
        assert hits[0].document["path"] == str(docs.resolve() / "sub" / "bravo.md")
    finally:
        folder.close()


def test_full_run_gives_both_stores_the_same_ids(docs, vault_home, fake_embedder):
    folder = _open(docs)
    status = folder_index.index_folder_now(folder)
    assert status["complete"]
    assert status["files_embedded"] == 2
    whoosh_ids = _whoosh_ids(folder)
    assert whoosh_ids
    assert _lance_ids(folder) == whoosh_ids
    recorded = vault_metadata.load_embedding_config(folder.index_path)
    assert recorded["source"] == FAKE_EMBEDDING_SOURCE
    assert recorded["dimension"] == 32


def test_index_dir_is_never_indexed_and_hidden_dirs_are_skipped(
    docs, vault_home, fake_embedder
):
    (docs / ".git").mkdir()
    (docs / ".git" / "notes.txt").write_text(_text("hidden"))
    (docs / ".secret.md").write_text(_text("secret"))
    folder = _open(docs)
    folder_index.index_folder_now(folder, vectors=False)
    paths = set()
    with (
        WhooshFullTextIndex(get_whoosh_index_path(folder.index_path)) as ix,
        ix.ix.searcher() as searcher,
    ):
        paths = {fields["path"] for fields in searcher.all_stored_fields()}
    assert all("/." not in p for p in paths)
    assert len(paths) == 2


def test_file_names_with_glob_characters_are_indexed(docs, vault_home, fake_embedder):
    (docs / "report [draft].txt").write_text(_text("draftreport"))
    folder = _open(docs)
    status = folder_index.index_folder_now(folder, vectors=False)
    assert status["files_keyword"] == 3
    assert status["files_empty"] == 0


def test_edit_delete_and_rename_replace_exactly_that_file(
    docs, vault_home, fake_embedder
):
    folder = _open(docs)
    folder_index.index_folder_now(folder)
    before = _lance_ids(folder)
    bravo_before = _ids_for(before, folder, "sub/bravo.md")

    (docs / "alpha.txt").write_text(_text("alphachanged", 40))
    (docs / "sub" / "bravo.md").rename(docs / "sub" / "charlie.md")
    folder_index.index_folder_now(_open(docs))

    after_w, after_l = _whoosh_ids(folder), _lance_ids(folder)
    assert after_w == after_l
    assert not _ids_for(after_l, folder, "sub/bravo.md")
    assert len(_ids_for(after_l, folder, "sub/charlie.md")) == len(bravo_before)
    with WhooshFullTextIndex(get_whoosh_index_path(folder.index_path)) as ix:
        assert ix.text_search("alphachanged")
        assert not ix.text_search("alpha")

    (docs / "alpha.txt").unlink()
    status = folder_index.index_folder_now(_open(docs))
    assert status["files_total"] == 1
    assert not _ids_for(_lance_ids(folder), folder, "alpha.txt")


def test_touched_but_unchanged_file_is_not_reindexed(docs, vault_home, fake_embedder):
    folder_index.index_folder_now(_open(docs))
    calls = fake_embedder["calls"]
    later = time.time() + 5
    os.utime(docs / "alpha.txt", (later, later))
    status = folder_index.index_folder_now(_open(docs))
    assert status["complete"]
    assert fake_embedder["calls"] == calls


def test_same_size_content_change_is_reindexed(docs, vault_home, fake_embedder):
    folder = _open(docs)
    folder_index.index_folder_now(folder)
    original = (docs / "alpha.txt").read_text()
    changed = original.replace("alpha", "omega", 1)
    assert len(changed) == len(original)
    (docs / "alpha.txt").write_text(changed)
    folder_index.index_folder_now(_open(docs))
    with WhooshFullTextIndex(get_whoosh_index_path(folder.index_path)) as ix:
        assert ix.text_search("omega")


def test_crash_between_commit_and_manifest_resumes_without_duplicates(
    docs, vault_home, fake_embedder, monkeypatch
):
    folder = _open(docs)
    assert folder.try_become_writer(watch=False)
    folder.scan()

    def crash(self, results):
        raise RuntimeError("simulated crash after the Whoosh commit")

    monkeypatch.setattr(folder_index.Manifest, "mark_keyword", crash)
    with pytest.raises(RuntimeError):
        folder.keyword_batch()
    committed = _whoosh_ids(folder)
    assert committed
    folder.close()
    monkeypatch.undo()
    monkeypatch.setenv("TALKPIPE_VAULT_HOME", str(vault_home))

    status = folder_index.index_folder_now(_open(docs))
    assert status["complete"]
    assert _whoosh_ids(folder) == committed
    assert _lance_ids(folder) == committed


def test_embedder_failure_keeps_keywords_and_resumes(docs, vault_home, fake_embedder):
    fake_embedder["fail_after"] = 0
    folder = _open(docs)
    status = folder_index.index_folder_now(folder)
    assert status["files_keyword"] == 2
    assert status["files_embedded"] == 0
    assert "fake embedder is down" in status["vector_error"]

    fake_embedder["fail_after"] = None
    status = folder_index.index_folder_now(_open(docs))
    assert status["complete"]
    assert _lance_ids(folder) == _whoosh_ids(folder)


def test_reembed_replaces_vectors_and_keeps_keywords(docs, vault_home, fake_embedder):
    folder = _open(docs)
    folder_index.index_folder_now(folder)
    folder = _open(docs)
    assert folder.try_become_writer(watch=False)
    try:
        folder.reembed(FAKE_EMBEDDING_SOURCE, "fake-two")
        assert not _lance_ids(folder)
        assert _whoosh_ids(folder)
        for _ in range(10):
            if not folder.has_vector_work(time.monotonic()):
                break
            folder.vector_window()
        assert folder.status()["complete"], folder.status()["vector_error"]
    finally:
        folder.close()
    recorded = vault_metadata.load_embedding_config(folder.index_path)
    assert recorded["model"] == "fake-two"


def test_recorded_embedder_wins_over_the_requested_one(docs, vault_home, fake_embedder):
    folder_index.index_folder_now(_open(docs))
    reopened = folder_index.open_folder(
        docs, embedding_source="ollama", embedding_model="something-else"
    )
    assert reopened.embedding_source == FAKE_EMBEDDING_SOURCE


# -- several processes -------------------------------------------------------------


def test_second_instance_is_a_reader_until_the_writer_leaves(
    docs, vault_home, fake_embedder
):
    first, second = _open(docs), _open(docs)
    assert first.try_become_writer(watch=False)
    assert not second.try_become_writer(watch=False)
    first.scan()
    first.write_status(force=True)
    reader_status = second.status()
    assert reader_status["role"] == "reader"
    assert "another vault process" in reader_status["writer"]
    assert reader_status["files_total"] == 2
    first.close()
    second._next_lock_try = 0
    assert second.try_become_writer(watch=False)
    second.close()


def _hold_lock(index_path, ready):
    import filelock

    lock = filelock.FileLock(
        os.path.join(index_path, folder_index.WRITER_LOCK_FILENAME),
        is_singleton=False,
    )
    lock.acquire()
    ready.set()
    time.sleep(60)


def test_killed_writer_releases_the_folder(docs, vault_home, fake_embedder):
    folder = _open(docs)
    Path(folder.index_path).mkdir(exist_ok=True)
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    child = ctx.Process(target=_hold_lock, args=(folder.index_path, ready))
    child.start()
    try:
        assert ready.wait(30)
        assert not folder.try_become_writer(watch=False)
        os.kill(child.pid, signal.SIGKILL)
        child.join(10)
        folder._next_lock_try = 0
        assert folder.try_become_writer(watch=False)
    finally:
        if child.is_alive():
            child.kill()
        folder.close()


# -- watching ----------------------------------------------------------------------


def test_change_events_ignore_hidden_and_index_paths(docs, vault_home, fake_embedder):
    folder = _open(docs)
    root = folder.root
    folder.note_change(str(root / FOLDER_INDEX_DIRNAME / "docs.lance" / "x.lance"))
    folder.note_change(str(root / ".git" / "index"))
    folder.note_change(str(root / "notes.txt.swp"))
    folder.note_change("/somewhere/else.txt")
    assert not folder._dirty
    folder.note_change(str(root / "sub" / "new.md"))
    assert folder._dirty == {"sub/new.md"}


def _wait_for(folder, predicate, timeout=60.0):
    """Wait for the background indexer; an error it reports fails at once."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = folder.status()
        failure = status["error"] or status["vector_error"]
        assert not failure, failure
        if predicate(status):
            return True
        time.sleep(0.2)
    return False


def test_scheduler_picks_up_a_new_file(docs, vault_home, fake_embedder):
    scheduler = folder_index.FolderScheduler(ingest_lock_dir=vault_home)
    folder = _open(docs)
    scheduler.add(folder)
    scheduler.start()
    try:
        assert _wait_for(folder, lambda s: s["complete"])
        assert folder.watch_mode == "live"
        (docs / "delta.txt").write_text(_text("delta"))
        assert _wait_for(folder, lambda s: s["files_embedded"] == 3)
    finally:
        scheduler.stop()
    assert not folder.is_writer


# -- the segments ------------------------------------------------------------------


def test_index_folder_segment(docs, vault_home, fake_embedder):
    segment = folder_index.index_folder(
        embedding_source=FAKE_EMBEDDING_SOURCE, embedding_model="fake"
    )
    [status] = list(segment.transform([str(docs)]))
    assert status["complete"]
    assert status["root"] == str(docs.resolve())


def test_index_folder_segment_compiles_in_chatterlang(docs, vault_home, fake_embedder):
    from talkpipe.chatterlang import compiler

    fn = compiler.compile(
        f'| indexFolder[embedding_source="{FAKE_EMBEDDING_SOURCE}", '
        'embedding_model="fake", vectors=False]'
    ).as_function(single_in=True, single_out=True)
    status = fn(str(docs))
    assert status["files_keyword"] == 2
    assert status["files_embedded"] == 0


def test_watch_folders_source_reports_progress(docs, vault_home, fake_embedder):
    source = folder_index.watch_folders(
        paths=str(docs),
        embedding_source=FAKE_EMBEDDING_SOURCE,
        embedding_model="fake",
        interval=0.1,
        max_events=50,
    )
    seen = []
    deadline = time.monotonic() + 60
    for status in source.generate():
        seen.append(status)
        # An error ends the wait too: the folder would never finish.
        if status["complete"] or status["error"] or status["vector_error"]:
            break
        assert time.monotonic() < deadline, seen[-1]
    assert seen[-1]["complete"], seen[-1]
    assert seen[-1]["role"] == "writer"
