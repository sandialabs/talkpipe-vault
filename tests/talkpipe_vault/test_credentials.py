"""Unit tests for vault-scoped provider credentials."""

import json
import os
import stat

import pytest

from talkpipe_vault.apps import credentials, user_settings

_ENV_VARS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "TALKPIPE_OLLAMA_SERVER_URL",
)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Isolate the credentials store and managed env between tests."""
    monkeypatch.setenv(user_settings.VAULT_HOME_ENV, str(tmp_path / "home"))
    for env_var in _ENV_VARS:
        # monkeypatch records the original so its teardown also removes any
        # value apply() writes directly to os.environ during the test.
        monkeypatch.delenv(env_var, raising=False)
    credentials._managed_env.clear()
    yield
    credentials._managed_env.clear()


def test_set_values_persists_and_applies_to_env():
    credentials.set_values(
        {
            "openai_api_key": "sk-live-123456",
            "openai_base_url": "https://proxy.example/v1",
            "ollama_server_url": "http://ollama.example:11434",
        }
    )
    assert os.environ["OPENAI_API_KEY"] == "sk-live-123456"
    assert os.environ["OPENAI_BASE_URL"] == "https://proxy.example/v1"
    assert os.environ["TALKPIPE_OLLAMA_SERVER_URL"] == "http://ollama.example:11434"


def test_credentials_file_is_owner_only():
    credentials.set_values({"openai_api_key": "sk-live-123456"})
    path = credentials.store_path()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    # Secret is stored, but only in the protected file.
    assert json.loads(path.read_text())["openai_api_key"] == "sk-live-123456"


def test_clearing_a_value_unsets_its_env_var():
    credentials.set_values({"openai_api_key": "sk-live-123456"})
    assert "OPENAI_API_KEY" in os.environ
    credentials.set_values({"openai_api_key": ""})
    assert "OPENAI_API_KEY" not in os.environ
    assert "openai_api_key" not in credentials.load()


def test_apply_does_not_clobber_unmanaged_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-deployment")
    # Nothing stored for OpenAI, so apply() must leave the deployment var alone.
    credentials.set_values({"ollama_server_url": "http://ollama.example:11434"})
    assert os.environ["OPENAI_API_KEY"] == "sk-from-deployment"


def test_stored_value_overrides_preexisting_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-deployment")
    credentials.set_values({"openai_api_key": "sk-from-ui-9999"})
    assert os.environ["OPENAI_API_KEY"] == "sk-from-ui-9999"


def test_source_for_reports_vault_vs_environment(monkeypatch):
    assert credentials.source_for("ANTHROPIC_API_KEY") == "unset"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    assert credentials.source_for("ANTHROPIC_API_KEY") == "environment"
    credentials.set_values({"anthropic_api_key": "sk-ant-ui-1234"})
    assert credentials.source_for("ANTHROPIC_API_KEY") == "Vault settings"


def test_describe_masks_secrets_and_exposes_urls():
    credentials.set_values(
        {
            "openai_api_key": "sk-secret-abcd",
            "ollama_server_url": "http://ollama.example:11434",
        }
    )
    rows = {row["key"]: row for row in credentials.describe()}

    secret_row = rows["openai_api_key"]
    assert secret_row["present"] is True
    assert secret_row["value"] == ""  # never echoed back
    assert secret_row["masked"].endswith("abcd")
    assert "sk-secret-abcd" not in secret_row["masked"]

    url_row = rows["ollama_server_url"]
    assert url_row["value"] == "http://ollama.example:11434"  # safe to prefill


def test_load_ignores_unreadable_file(tmp_path, monkeypatch):
    path = credentials.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json {")
    assert credentials.load() == {}
