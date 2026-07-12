"""Persistent user settings for the vault web application.

Stores the recent-vault list and model configuration chosen in the web
interface as JSON under the vault application home directory
(``~/.talkpipe-vault`` by default, overridable via the
``TALKPIPE_VAULT_HOME`` environment variable).

Model values saved here act as user overrides: ``None``/absent values fall
through to TalkPipe configuration and then to the vault defaults (see
``talkpipe_vault.pipelines.config``).
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

VAULT_HOME_ENV = "TALKPIPE_VAULT_HOME"
DEFAULT_VAULT_HOME = "~/.talkpipe-vault"
SETTINGS_FILENAME = "settings.json"
MAX_RECENT_VAULTS = 10

MODEL_SETTING_KEYS = (
    "embedding_model",
    "embedding_source",
    "chat_model",
    "chat_source",
)

INTEGER_SETTING_MINIMUMS = {
    "chunk_size": 1,
    "shingle_size": 1,
    "shingle_overlap": 0,
    "rag_result_limit": 1,
}


def get_vault_home() -> Path:
    """Return the vault application home directory (not necessarily existing)."""
    return Path(os.environ.get(VAULT_HOME_ENV) or DEFAULT_VAULT_HOME).expanduser()


def _settings_path() -> Path:
    return get_vault_home() / SETTINGS_FILENAME


def load_settings() -> dict:
    """Load persisted settings, returning an empty structure when unavailable."""
    path = _settings_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("settings root is not an object")
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable settings file %s: %s", path, exc)
        data = {}

    data.setdefault("recent_vaults", [])
    if not isinstance(data["recent_vaults"], list):
        data["recent_vaults"] = []
    return data


def save_settings(settings: dict) -> None:
    """Persist settings as JSON, creating the vault home directory if needed."""
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    tmp_path.replace(path)


def get_recent_vaults() -> list[str]:
    """Return known vault paths, most recently used first."""
    return [str(p) for p in load_settings()["recent_vaults"]]


def remember_vault(vault_path: str) -> None:
    """Record a vault path at the head of the recent-vault list."""
    resolved = str(Path(vault_path).expanduser())
    settings = load_settings()
    recents = [p for p in settings["recent_vaults"] if p != resolved]
    recents.insert(0, resolved)
    settings["recent_vaults"] = recents[:MAX_RECENT_VAULTS]
    save_settings(settings)


def get_model_overrides() -> dict:
    """Return saved settings overrides; absent/blank values are omitted."""
    settings = load_settings()
    overrides = {}
    for key in MODEL_SETTING_KEYS:
        value = settings.get(key)
        if isinstance(value, str) and value.strip():
            overrides[key] = value.strip()
    for key, minimum in INTEGER_SETTING_MINIMUMS.items():
        value = settings.get(key)
        try:
            numeric_value = int(value)
        except (TypeError, ValueError):
            continue
        if numeric_value >= minimum:
            overrides[key] = numeric_value
    return overrides


def save_model_overrides(**overrides: str | int | None) -> None:
    """Persist settings overrides; blank/None values clear the override."""
    settings = load_settings()
    for key in MODEL_SETTING_KEYS:
        if key not in overrides:
            continue
        value = overrides[key]
        if isinstance(value, str) and value.strip():
            settings[key] = value.strip()
        else:
            settings.pop(key, None)
    for key, minimum in INTEGER_SETTING_MINIMUMS.items():
        if key not in overrides:
            continue
        value = overrides[key]
        if value is None or (isinstance(value, str) and not value.strip()):
            settings.pop(key, None)
            continue
        try:
            numeric_value = int(value)
        except (TypeError, ValueError):
            settings.pop(key, None)
            continue
        if numeric_value >= minimum:
            settings[key] = numeric_value
        else:
            settings.pop(key, None)
    save_settings(settings)
