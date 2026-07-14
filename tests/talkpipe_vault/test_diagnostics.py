"""Unit tests for configuration diagnostics."""

import pytest

from talkpipe_vault.pipelines import diagnostics


def _find(report, name):
    """Return the check row with the given name."""
    for check in report["checks"]:
        if check["name"] == name:
            return check
    raise AssertionError(f"no check named {name!r} in {report}")


@pytest.fixture(autouse=True)
def isolate_env(tmp_path, monkeypatch):
    """Isolate settings home and clear provider keys for deterministic checks."""
    from talkpipe_vault.apps import user_settings

    monkeypatch.setenv(user_settings.VAULT_HOME_ENV, str(tmp_path / "vault-home"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TALKPIPE_OLLAMA_SERVER_URL", raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    yield


@pytest.fixture
def probe_ok(monkeypatch):
    """Make the functional probe succeed, to isolate prerequisite-check logic.

    Recognized providers now confirm themselves by actually embedding a string
    or running a chat turn. Tests that only care about the reachability/cache/
    key logic stub that final step so they neither touch the network nor load a
    real model.
    """
    monkeypatch.setattr(
        diagnostics,
        "_functional_probe",
        lambda role, key, model, timeout: (True, 384),
    )


def _models(**overrides):
    base = {
        "embedding_source": "model2vec",
        "embedding_model": "minishlab/potion-retrieval-32M",
        "chat_source": "ollama",
        "chat_model": "mistral-small",
    }
    base.update(overrides)
    return base


def test_model2vec_not_checked_when_probe_disabled():
    report = diagnostics.collect_config_status(_models(), probe=False)
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "unknown"


def test_model2vec_ready_when_cached(monkeypatch, probe_ok):
    monkeypatch.setattr(
        diagnostics, "_model2vec_cache_state", lambda model: ("ready", "/hf/cache/x")
    )
    report = diagnostics.collect_config_status(_models())
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "ok"
    assert "ready" in embeddings["summary"]
    assert "test string" in embeddings["summary"]
    assert "/hf/cache/x" in embeddings["detail"]


def test_model2vec_ready_but_embedding_fails_is_error(monkeypatch):
    monkeypatch.setattr(
        diagnostics, "_model2vec_cache_state", lambda model: ("ready", "/hf/cache/x")
    )
    monkeypatch.setattr(
        diagnostics,
        "_functional_probe",
        lambda role, key, model, timeout: (False, ValueError("corrupt weights")),
    )
    report = diagnostics.collect_config_status(_models())
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "error"
    assert "failed to produce a test embedding" in embeddings["summary"]
    assert "corrupt weights" in embeddings["detail"]


def test_model2vec_absent_online_warns_about_firewall(monkeypatch):
    monkeypatch.setattr(
        diagnostics, "_model2vec_cache_state", lambda model: ("absent", None)
    )
    report = diagnostics.collect_config_status(_models())
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "warn"
    assert "not available in the local cache" in embeddings["summary"]
    assert "firewall" in embeddings["summary"]
    assert "HF_HUB_OFFLINE=1" in embeddings["fix"]


def test_model2vec_absent_offline_is_error(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(
        diagnostics, "_model2vec_cache_state", lambda model: ("absent", None)
    )
    report = diagnostics.collect_config_status(_models())
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "error"
    assert "offline mode is on" in embeddings["summary"]
    assert "cannot be loaded from the local cache" in embeddings["summary"]


def test_model2vec_ready_offline_notes_no_network(monkeypatch, probe_ok):
    monkeypatch.setenv("HF_HUB_OFFLINE", "on")
    monkeypatch.setattr(
        diagnostics, "_model2vec_cache_state", lambda model: ("ready", "/hf/cache/x")
    )
    report = diagnostics.collect_config_status(_models())
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "ok"
    assert "offline mode is on" in embeddings["summary"]


def test_model2vec_ready_online_hints_offline_for_firewall(monkeypatch, probe_ok):
    monkeypatch.setattr(
        diagnostics, "_model2vec_cache_state", lambda model: ("ready", "/hf/cache/x")
    )
    report = diagnostics.collect_config_status(_models())
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "ok"
    assert "HF_HUB_OFFLINE=1" in embeddings["detail"]


def test_model2vec_missing_package_is_error(monkeypatch):
    monkeypatch.setattr(
        diagnostics, "_model2vec_cache_state", lambda model: ("missing_package", None)
    )
    report = diagnostics.collect_config_status(_models())
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "error"
    assert "not installed" in embeddings["summary"]
    assert "pip install" in embeddings["fix"]


def test_model2vec_local_directory_is_ok(tmp_path, probe_ok):
    report = diagnostics.collect_config_status(_models(embedding_model=str(tmp_path)))
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "ok"
    assert "Local model directory" in embeddings["summary"]


def test_model2vec_cache_state_reports_ready_for_cached_default():
    # The default model is expected to be cached in most dev/CI environments;
    # if it is not, the check must still degrade to "absent", never raise.
    state, _detail = diagnostics._model2vec_cache_state(
        "minishlab/potion-retrieval-32M"
    )
    assert state in {"ready", "absent", "missing_package"}


def test_ollama_unreachable_is_error_with_fix(monkeypatch):
    monkeypatch.setattr(
        diagnostics, "_ollama_tags", lambda url, timeout: (None, "Connection refused")
    )
    report = diagnostics.collect_config_status(_models())
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "error"
    assert "Can't reach Ollama" in chat["summary"]
    assert "TALKPIPE_OLLAMA_SERVER_URL" in chat["fix"]
    assert report["overall"] == "error"


def test_ollama_reachable_but_model_missing(monkeypatch):
    monkeypatch.setattr(
        diagnostics, "_ollama_tags", lambda url, timeout: (["llama3:latest"], None)
    )
    report = diagnostics.collect_config_status(_models())
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "error"
    assert "not pulled" in chat["summary"]
    assert chat["fix"] == "Run: ollama pull mistral-small"


def test_ollama_reachable_with_model_matches_base_tag(monkeypatch, probe_ok):
    monkeypatch.setattr(
        diagnostics,
        "_ollama_tags",
        lambda url, timeout: (["mistral-small:latest"], None),
    )
    report = diagnostics.collect_config_status(_models())
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "ok"
    assert "completed a test chat exchange" in chat["summary"]


def test_ollama_present_but_chat_probe_fails_is_error(monkeypatch):
    monkeypatch.setattr(
        diagnostics,
        "_ollama_tags",
        lambda url, timeout: (["mistral-small:latest"], None),
    )
    monkeypatch.setattr(
        diagnostics,
        "_functional_probe",
        lambda role, key, model, timeout: (False, RuntimeError("connection reset")),
    )
    report = diagnostics.collect_config_status(_models())
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "error"
    assert "a test chat exchange" in chat["summary"]
    assert "failed" in chat["summary"]
    assert "connection reset" in chat["detail"]


def test_openai_selected_without_key_is_error():
    report = diagnostics.collect_config_status(
        _models(chat_source="openai", chat_model="gpt-4o"),
        probe=False,
    )
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "error"
    assert "no OpenAI API key is set" in chat["summary"]


def test_openai_key_present_validates(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-testkey1234")
    monkeypatch.setattr(
        diagnostics,
        "_functional_probe",
        lambda role, key, model, timeout: (True, 1536),
    )
    report = diagnostics.collect_config_status(
        _models(chat_source="openai", chat_model="gpt-4o")
    )
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "ok"
    assert "key works" in chat["summary"]
    assert "succeeded" in chat["summary"]
    assert "1234" in chat["detail"]  # key is masked to last four


def test_openai_key_rejected_is_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-badkey6789")
    monkeypatch.setattr(
        diagnostics,
        "_functional_probe",
        lambda role, key, model, timeout: (
            False,
            RuntimeError("Error code: 401 - invalid_api_key"),
        ),
    )
    report = diagnostics.collect_config_status(
        _models(chat_source="openai", chat_model="gpt-4o")
    )
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "error"
    assert "rejected it" in chat["summary"]


def test_openai_probe_network_error_is_warn(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-testkey1234")
    monkeypatch.setattr(
        diagnostics,
        "_functional_probe",
        lambda role, key, model, timeout: (
            False,
            RuntimeError("Connection timed out"),
        ),
    )
    report = diagnostics.collect_config_status(
        _models(chat_source="openai", chat_model="gpt-4o")
    )
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "warn"
    assert "could not be completed" in chat["summary"]
    assert "Connection timed out" in chat["detail"]


def test_key_present_not_validated_when_probe_disabled(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-testkey1234")
    report = diagnostics.collect_config_status(
        _models(chat_source="openai", chat_model="gpt-4o"),
        probe=False,
    )
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "ok"
    assert "not validated" in chat["summary"]


def test_provenance_reflects_settings_override(monkeypatch, probe_ok):
    from talkpipe_vault.apps import user_settings

    user_settings.save_model_overrides(chat_source="ollama", chat_model="mistral-small")
    monkeypatch.setattr(
        diagnostics,
        "_ollama_tags",
        lambda url, timeout: (["mistral-small:latest"], None),
    )
    report = diagnostics.collect_config_status(_models())
    chat = _find(report, "Chat (Ask) provider")
    assert chat["source"] == "Settings page"


def test_ollama_url_provenance_from_env(monkeypatch, probe_ok):
    monkeypatch.setenv("TALKPIPE_OLLAMA_SERVER_URL", "http://example.test:11434")
    monkeypatch.setattr(
        diagnostics,
        "_ollama_tags",
        lambda url, timeout: (["mistral-small:latest"], None),
    )
    report = diagnostics.collect_config_status(_models())
    chat = _find(report, "Chat (Ask) provider")
    assert "example.test" in chat["detail"]
    assert "TALKPIPE_OLLAMA_SERVER_URL env var" in chat["detail"]


def test_no_embedding_match_row_without_vault():
    report = diagnostics.collect_config_status(_models(), probe=False)
    names = {check["name"] for check in report["checks"]}
    assert "Embedding ↔ index" not in names


def test_embedding_match_row_ok_when_matching():
    recorded = {
        "source": "model2vec",
        "model": "minishlab/potion-retrieval-32M",
        "dimension": 512,
    }
    report = diagnostics.collect_config_status(
        _models(), vault_selected=True, vault_embedding=recorded, probe=False
    )
    match = _find(report, "Embedding ↔ index")
    assert match["status"] == "ok"
    assert "512-dimension" in match["detail"]


def test_embedding_match_row_error_on_mismatch():
    recorded = {"source": "openai", "model": "text-embedding-3-large"}
    report = diagnostics.collect_config_status(
        _models(), vault_selected=True, vault_embedding=recorded, probe=False
    )
    match = _find(report, "Embedding ↔ index")
    assert match["status"] == "error"
    assert "indexed with openai/text-embedding-3-large" in match["summary"]
    assert "re-index" in match["fix"]
    assert report["overall"] == "error"


def test_embedding_match_row_unknown_for_legacy_vault():
    report = diagnostics.collect_config_status(
        _models(), vault_selected=True, vault_embedding=None, probe=False
    )
    match = _find(report, "Embedding ↔ index")
    assert match["status"] == "unknown"
    assert "no recorded embedding configuration" in match["summary"]


def test_api_key_provenance_shows_vault_settings(monkeypatch):
    from talkpipe_vault.apps import credentials

    credentials._managed_env.clear()
    credentials.set_values({"openai_api_key": "sk-vault-9999"})
    monkeypatch.setattr(
        diagnostics,
        "_functional_probe",
        lambda role, key, model, timeout: (True, 1536),
    )
    report = diagnostics.collect_config_status(
        _models(chat_source="openai", chat_model="gpt-4o")
    )
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "ok"
    assert "from Vault settings" in chat["detail"]
    credentials._managed_env.clear()


def test_model_present_matching():
    assert diagnostics._model_present("mistral-small", ["mistral-small:latest"])
    assert diagnostics._model_present("mistral-small:latest", ["mistral-small:latest"])
    assert not diagnostics._model_present("mistral-small", ["llama3:latest"])
    assert not diagnostics._model_present("", ["mistral-small:latest"])


def test_mask_reveals_only_tail():
    assert diagnostics._mask("sk-abcdefghij") == "…ghij"
    assert diagnostics._mask("short") == "****"


def test_is_auth_failure_matches_keywords_and_chain():
    # Direct message keyword.
    assert diagnostics._is_auth_failure("openai", RuntimeError("401 Unauthorized"))
    assert diagnostics._is_auth_failure(
        "openai", ValueError("incorrect API key provided")
    )
    # Wrapped in a cause chain.
    inner = RuntimeError("invalid_api_key")
    outer = RuntimeError("request failed")
    outer.__cause__ = inner
    assert diagnostics._is_auth_failure("openai", outer)
    # Unrelated failures are not auth failures.
    assert not diagnostics._is_auth_failure("openai", RuntimeError("connection reset"))


@pytest.fixture
def register_fake_adapters():
    """Register throwaway embedding/chat adapters for a custom provider name.

    Yields a small controller so a test can make each adapter succeed or raise,
    then unregisters them so the global TalkPipe registry stays clean.
    """
    from talkpipe.llm import config as llm_config

    behavior = {"embed": lambda: [0.0] * 8, "chat": lambda: "ok"}

    class _FakeEmbeddingAdapter:
        def __init__(self, model=None):
            self.model = model

        def execute_one(self, text):
            return behavior["embed"]()

    class _FakeChatAdapter:
        def __init__(self, model=None):
            self.model = model

        def execute(self, prompt):
            return behavior["chat"]()

    llm_config.registerEmbeddingAdapter("customprov", _FakeEmbeddingAdapter)
    llm_config.registerPromptAdapter("customprov", _FakeChatAdapter)
    try:
        yield behavior
    finally:
        llm_config._embeddingAdapter.pop("customprov", None)
        llm_config._promptAdapter.pop("customprov", None)


def test_unregistered_provider_stays_warn():
    report = diagnostics.collect_config_status(
        _models(embedding_source="totally-made-up")
    )
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "warn"
    assert "Can't automatically verify" in embeddings["summary"]


def test_unrecognized_provider_not_probed_when_disabled(register_fake_adapters):
    report = diagnostics.collect_config_status(
        _models(embedding_source="customprov"), probe=False
    )
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "unknown"
    assert "not exercised" in embeddings["summary"]


def test_unrecognized_embedding_provider_ok_when_probe_succeeds(register_fake_adapters):
    report = diagnostics.collect_config_status(_models(embedding_source="customprov"))
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "ok"
    assert "test embedding succeeded" in embeddings["summary"]
    assert "8-dimension" in embeddings["summary"]


def test_unrecognized_chat_provider_ok_when_probe_succeeds(register_fake_adapters):
    report = diagnostics.collect_config_status(
        _models(chat_source="customprov", chat_model="whatever")
    )
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "ok"
    assert "test chat exchange succeeded" in chat["summary"]


def test_unrecognized_chat_provider_error_when_probe_fails(register_fake_adapters):
    def boom():
        raise RuntimeError("model not loaded")

    register_fake_adapters["chat"] = boom
    report = diagnostics.collect_config_status(
        _models(chat_source="customprov", chat_model="whatever")
    )
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "error"
    assert "test chat exchange failed" in chat["summary"]
    assert "model not loaded" in chat["detail"]


def test_unrecognized_embedding_provider_error_on_empty_vector(register_fake_adapters):
    register_fake_adapters["embed"] = lambda: []
    report = diagnostics.collect_config_status(_models(embedding_source="customprov"))
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "error"
    assert "test embedding failed" in embeddings["summary"]
    assert "empty embedding vector" in embeddings["detail"]


def test_functional_probe_times_out_gracefully(monkeypatch):
    # A hanging provider must not hang Settings: the bounded runner returns an
    # error instead of blocking forever.
    monkeypatch.setattr(diagnostics, "FUNCTIONAL_PROBE_TIMEOUT", 0.1)

    def slow():
        import time

        time.sleep(5)
        return 3

    ok, result = diagnostics._run_bounded(slow, 0.1)
    assert ok is False
    assert isinstance(result, TimeoutError)
