"""Tests for vault-home resolution in user_settings."""

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
