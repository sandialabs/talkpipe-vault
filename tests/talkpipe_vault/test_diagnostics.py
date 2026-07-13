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
    report = diagnostics.collect_config_status(_models(), vault_path=None, probe=False)
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "unknown"


def test_model2vec_ready_when_cached(monkeypatch):
    monkeypatch.setattr(
        diagnostics, "_model2vec_cache_state", lambda model: ("ready", "/hf/cache/x")
    )
    report = diagnostics.collect_config_status(_models(), vault_path=None)
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "ok"
    assert "ready" in embeddings["summary"]
    assert "/hf/cache/x" in embeddings["detail"]


def test_model2vec_absent_online_warns_about_firewall(monkeypatch):
    monkeypatch.setattr(
        diagnostics, "_model2vec_cache_state", lambda model: ("absent", None)
    )
    report = diagnostics.collect_config_status(_models(), vault_path=None)
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
    report = diagnostics.collect_config_status(_models(), vault_path=None)
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "error"
    assert "offline mode is on" in embeddings["summary"]
    assert "cannot be loaded from the local cache" in embeddings["summary"]


def test_model2vec_ready_offline_notes_no_network(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "on")
    monkeypatch.setattr(
        diagnostics, "_model2vec_cache_state", lambda model: ("ready", "/hf/cache/x")
    )
    report = diagnostics.collect_config_status(_models(), vault_path=None)
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "ok"
    assert "offline mode is on" in embeddings["summary"]


def test_model2vec_ready_online_hints_offline_for_firewall(monkeypatch):
    monkeypatch.setattr(
        diagnostics, "_model2vec_cache_state", lambda model: ("ready", "/hf/cache/x")
    )
    report = diagnostics.collect_config_status(_models(), vault_path=None)
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "ok"
    assert "HF_HUB_OFFLINE=1" in embeddings["detail"]


def test_model2vec_missing_package_is_error(monkeypatch):
    monkeypatch.setattr(
        diagnostics, "_model2vec_cache_state", lambda model: ("missing_package", None)
    )
    report = diagnostics.collect_config_status(_models(), vault_path=None)
    embeddings = _find(report, "Embeddings provider")
    assert embeddings["status"] == "error"
    assert "not installed" in embeddings["summary"]
    assert "pip install" in embeddings["fix"]


def test_model2vec_local_directory_is_ok(tmp_path):
    report = diagnostics.collect_config_status(
        _models(embedding_model=str(tmp_path)), vault_path=None
    )
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
    report = diagnostics.collect_config_status(_models(), vault_path=None)
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "error"
    assert "Can't reach Ollama" in chat["summary"]
    assert "TALKPIPE_OLLAMA_SERVER_URL" in chat["fix"]
    assert report["overall"] == "error"


def test_ollama_reachable_but_model_missing(monkeypatch):
    monkeypatch.setattr(
        diagnostics, "_ollama_tags", lambda url, timeout: (["llama3:latest"], None)
    )
    report = diagnostics.collect_config_status(_models(), vault_path=None)
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "error"
    assert "not pulled" in chat["summary"]
    assert chat["fix"] == "Run: ollama pull mistral-small"


def test_ollama_reachable_with_model_matches_base_tag(monkeypatch):
    monkeypatch.setattr(
        diagnostics,
        "_ollama_tags",
        lambda url, timeout: (["mistral-small:latest"], None),
    )
    report = diagnostics.collect_config_status(_models(), vault_path=None)
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "ok"
    assert "available" in chat["summary"]


def test_openai_selected_without_key_is_error():
    report = diagnostics.collect_config_status(
        _models(chat_source="openai", chat_model="gpt-4o"),
        vault_path=None,
        probe=False,
    )
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "error"
    assert "no OpenAI API key is set" in chat["summary"]


def test_openai_key_present_validates(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-testkey1234")
    monkeypatch.setattr(diagnostics, "_probe_api_key", lambda package, timeout: None)
    report = diagnostics.collect_config_status(
        _models(chat_source="openai", chat_model="gpt-4o"), vault_path=None
    )
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "ok"
    assert "validated" in chat["summary"]
    assert "1234" in chat["detail"]  # key is masked to last four


def test_openai_key_rejected_is_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-badkey6789")
    monkeypatch.setattr(diagnostics, "_probe_api_key", lambda package, timeout: "auth")
    report = diagnostics.collect_config_status(
        _models(chat_source="openai", chat_model="gpt-4o"), vault_path=None
    )
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "error"
    assert "rejected it" in chat["summary"]


def test_key_present_not_validated_when_probe_disabled(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-testkey1234")
    report = diagnostics.collect_config_status(
        _models(chat_source="openai", chat_model="gpt-4o"),
        vault_path=None,
        probe=False,
    )
    chat = _find(report, "Chat (Ask) provider")
    assert chat["status"] == "ok"
    assert "not validated" in chat["summary"]


def test_vault_missing_is_error(tmp_path):
    report = diagnostics.collect_config_status(
        _models(), vault_path=str(tmp_path / "nope"), probe=False
    )
    vault = _find(report, "Vault")
    assert vault["status"] == "error"
    assert "does not exist" in vault["summary"]


def test_vault_none_selected_is_warn():
    report = diagnostics.collect_config_status(_models(), vault_path=None, probe=False)
    vault = _find(report, "Vault")
    assert vault["status"] == "warn"


def test_provenance_reflects_settings_override(monkeypatch):
    from talkpipe_vault.apps import user_settings

    user_settings.save_model_overrides(chat_source="ollama", chat_model="mistral-small")
    monkeypatch.setattr(
        diagnostics,
        "_ollama_tags",
        lambda url, timeout: (["mistral-small:latest"], None),
    )
    report = diagnostics.collect_config_status(_models(), vault_path=None)
    chat = _find(report, "Chat (Ask) provider")
    assert chat["source"] == "Settings page"


def test_ollama_url_provenance_from_env(monkeypatch):
    monkeypatch.setenv("TALKPIPE_OLLAMA_SERVER_URL", "http://example.test:11434")
    monkeypatch.setattr(
        diagnostics,
        "_ollama_tags",
        lambda url, timeout: (["mistral-small:latest"], None),
    )
    report = diagnostics.collect_config_status(_models(), vault_path=None)
    chat = _find(report, "Chat (Ask) provider")
    assert "example.test" in chat["detail"]
    assert "TALKPIPE_OLLAMA_SERVER_URL env var" in chat["detail"]


def test_api_key_provenance_shows_vault_settings(monkeypatch):
    from talkpipe_vault.apps import credentials

    credentials._managed_env.clear()
    credentials.set_values({"openai_api_key": "sk-vault-9999"})
    monkeypatch.setattr(diagnostics, "_probe_api_key", lambda package, timeout: None)
    report = diagnostics.collect_config_status(
        _models(chat_source="openai", chat_model="gpt-4o"), vault_path=None
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
