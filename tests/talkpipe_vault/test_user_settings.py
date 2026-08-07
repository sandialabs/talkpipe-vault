"""Tests for vault-home resolution and per-vault flags in user_settings."""

import pytest

from talkpipe_vault.apps import user_settings


def test_env_var_takes_precedence(monkeypatch):
    monkeypatch.setenv(user_settings.VAULT_HOME_ENV, "/from/env")
    monkeypatch.setattr(
        user_settings, "get_config", lambda: {"VAULT_HOME": "/from/toml"}
    )
    assert user_settings.get_vault_home() == user_settings.Path("/from/env")


def test_falls_back_to_talkpipe_config(monkeypatch):
    monkeypatch.delenv(user_settings.VAULT_HOME_ENV, raising=False)
    monkeypatch.setattr(
        user_settings, "get_config", lambda: {"VAULT_HOME": "~/custom-vault-home"}
    )
    expected = user_settings.Path("~/custom-vault-home").expanduser()
    assert user_settings.get_vault_home() == expected


def test_falls_back_to_default_when_unset(monkeypatch):
    monkeypatch.delenv(user_settings.VAULT_HOME_ENV, raising=False)
    monkeypatch.setattr(user_settings, "get_config", lambda: {})
    expected = user_settings.Path(user_settings.DEFAULT_VAULT_HOME).expanduser()
    assert user_settings.get_vault_home() == expected


def test_config_load_failure_falls_back_to_default(monkeypatch):
    monkeypatch.delenv(user_settings.VAULT_HOME_ENV, raising=False)

    def _boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(user_settings, "get_config", _boom)
    expected = user_settings.Path(user_settings.DEFAULT_VAULT_HOME).expanduser()
    assert user_settings.get_vault_home() == expected


class TestRetrievalFilterFlags:
    """Retrieval-filter activation is a per-machine choice, made per vault."""

    @pytest.fixture(autouse=True)
    def isolated_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv(user_settings.VAULT_HOME_ENV, str(tmp_path / "home"))

    def test_unknown_vault_defaults_to_disabled(self, tmp_path):
        """A vault copied from elsewhere never runs its script unasked."""
        assert user_settings.get_retrieval_filter_flags(str(tmp_path)) == {
            "enabled": False,
            "strict": False,
        }

    def test_flags_round_trip(self, tmp_path):
        user_settings.set_retrieval_filter_flags(
            str(tmp_path), enabled=True, strict=True
        )
        assert user_settings.get_retrieval_filter_flags(str(tmp_path)) == {
            "enabled": True,
            "strict": True,
        }

    def test_flags_are_kept_per_vault(self, tmp_path):
        one, two = tmp_path / "one", tmp_path / "two"
        user_settings.set_retrieval_filter_flags(str(one), enabled=True, strict=False)
        user_settings.set_retrieval_filter_flags(str(two), enabled=False, strict=True)

        assert user_settings.get_retrieval_filter_flags(str(one))["enabled"] is True
        assert user_settings.get_retrieval_filter_flags(str(two))["enabled"] is False

    def test_clearing_restores_the_disabled_default(self, tmp_path):
        user_settings.set_retrieval_filter_flags(
            str(tmp_path), enabled=True, strict=True
        )
        user_settings.clear_retrieval_filter_flags(str(tmp_path))

        assert user_settings.get_retrieval_filter_flags(str(tmp_path))["enabled"] is False

    def test_clearing_an_unknown_vault_is_harmless(self, tmp_path):
        user_settings.clear_retrieval_filter_flags(str(tmp_path))

    def test_flags_survive_other_settings_changes(self, tmp_path):
        user_settings.set_retrieval_filter_flags(
            str(tmp_path), enabled=True, strict=False
        )
        user_settings.remember_vault(str(tmp_path))

        assert user_settings.get_retrieval_filter_flags(str(tmp_path))["enabled"] is True
