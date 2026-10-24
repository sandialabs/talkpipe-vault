"""Tests for searching folder indexes, alone and several together."""

import shutil

import pytest
from talkpipe.search.abstract import SearchResult

from talkpipe_vault.pipelines import folder_index
from talkpipe_vault.pipelines.config import FOLDER_INDEX_DIRNAME
from talkpipe_vault.pipelines.searching_and_prompting import (
    MergeSearchResults,
    VaultChat,
    VaultSearch,
    VaultTextSearch,
    reciprocal_rank_fusion,
    resolve_vault_paths,
    words_query,
)
from tests.conftest import FAKE_EMBEDDING_SOURCE


@pytest.fixture
def vault_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("TALKPIPE_VAULT_HOME", str(home))
    monkeypatch.delenv("TALKPIPE_VAULT_ROOT", raising=False)
    return home


def _folder(base, name, files):
    root = base / name
    root.mkdir(parents=True)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _index(root, vectors=True):
    folder = folder_index.open_folder(
        root, embedding_source=FAKE_EMBEDDING_SOURCE, embedding_model="fake"
    )
    return folder_index.index_folder_now(folder, vectors=vectors)


def _search(paths, **kwargs):
    return VaultSearch(
        vault_path=paths,
        embedding_source=FAKE_EMBEDDING_SOURCE,
        embedding_model="fake",
        **kwargs,
    ).as_function(single_in=True, single_out=True)


@pytest.fixture
def two_folders(tmp_path, vault_home, fake_embedder):
    work = _folder(
        tmp_path / "a",
        "notes",
        {
            "README.md": "Lighthouse keeper logbook for the northern harbour.\n",
            "ships.txt": "The schooner Wanderer docked at dawn with copper.\n",
        },
    )
    home = _folder(
        tmp_path / "b",
        "notes",
        {
            "README.md": "Garden journal: tomatoes, basil and the old greenhouse.\n",
            "recipes.txt": "Basil pesto with pine nuts and garlic.\n",
        },
    )
    _index(work)
    _index(home)
    return work, home


def test_reciprocal_rank_fusion_rewards_agreement():
    a = [SearchResult(score=9, doc_id="x"), SearchResult(score=8, doc_id="y")]
    b = [SearchResult(score=1, doc_id="y"), SearchResult(score=0.5, doc_id="z")]
    fused = reciprocal_rank_fusion([a, b])
    assert [r.doc_id for r in fused] == ["y", "x", "z"]
    assert fused[0].score == 8  # each result keeps its own score
    assert [r.doc_id for r in reciprocal_rank_fusion([a, b], limit=1)] == ["y"]


def test_merge_search_results_rrf_strategy():
    merge = MergeSearchResults(field_list="v,k", set_as="out", strategy="rrf")
    item = {
        "v": [SearchResult(score=1, doc_id="x"), SearchResult(score=1, doc_id="y")],
        "k": [{"doc_id": "y", "score": 3.0, "document": {"path": "/p"}}],
    }
    [out] = list(merge.transform([item]))
    assert [r.doc_id for r in out["out"]] == ["y", "x"]
    with pytest.raises(ValueError):
        MergeSearchResults(field_list="v", set_as="o", strategy="bogus")


def test_words_query_ors_the_longer_words():
    assert words_query("What is the lighthouse log?") == (
        '"What" OR "the" OR "lighthouse" OR "log"'
    )


def test_a_documents_folder_stands_for_its_index(two_folders):
    work, _ = two_folders
    [resolved] = resolve_vault_paths(str(work))
    assert resolved == str(work.resolve() / FOLDER_INDEX_DIRNAME)


def test_semantic_search_spans_folders_and_labels_hits(two_folders):
    work, home = two_folders
    results = _search([str(work), str(home)], limit=10)("basil garden")
    labels = {r.document["folder"] for r in results}
    assert labels == {"notes (a)", "notes (b)"}
    sources = {r.document["source"] for r in results}
    # The same relative path in both folders stays two separate hits.
    assert str(work.resolve() / "README.md") in sources
    assert str(home.resolve() / "README.md") in sources
    assert len({r.doc_id for r in results}) == len(results)


def test_keyword_search_spans_folders(two_folders):
    work, home = two_folders
    search = VaultTextSearch(vault_path=[str(work), str(home)], limit=10)
    hits = search.process_value("basil OR schooner")
    paths = {hit["document"]["path"] for hit in hits}
    assert paths == {
        str(work.resolve() / "ships.txt"),
        str(home.resolve() / "README.md"),
        str(home.resolve() / "recipes.txt"),
    }


def test_keyword_fallback_finds_files_not_yet_embedded(
    tmp_path, vault_home, fake_embedder
):
    root = _folder(tmp_path, "docs", {"log.txt": "The lighthouse lamp failed.\n"})
    _index(root, vectors=False)
    assert _search(str(root))("lighthouse lamp") == []
    results = _search(str(root), keyword_fallback=True)("lighthouse lamp")
    assert results
    assert results[0].document["source"] == str(root.resolve() / "log.txt")


def test_hits_from_a_moved_folder_point_at_its_new_place(
    tmp_path, vault_home, fake_embedder
):
    root = _folder(tmp_path, "old", {"log.txt": "The lighthouse lamp failed.\n"})
    _index(root)
    moved = tmp_path / "new"
    shutil.move(str(root), str(moved))
    [hit] = _search(str(moved), limit=1)("lighthouse")
    assert hit.document["source"] == str(moved.resolve() / "log.txt")
    [keyword_hit] = VaultTextSearch(vault_path=str(moved)).process_value("lamp")
    assert keyword_hit["document"]["path"] == str(moved.resolve() / "log.txt")


def test_chat_answers_from_several_folders(two_folders):
    work, home = two_folders
    chat = VaultChat(
        vault_path=[str(work), str(home)],
        embedding_source=FAKE_EMBEDDING_SOURCE,
        embedding_model="fake",
        chat_source="eliza",
        chat_model="eliza",
        include_background=True,
        limit=4,
    ).as_function(single_in=True, single_out=True)
    result = chat("Where did the schooner dock?")
    assert result["response"]
    assert {r.document["folder"] for r in result["background"]} == {
        "notes (a)",
        "notes (b)",
    }
