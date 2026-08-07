"""Persistent user settings for the vault web application.

Stores the recent-vault list and model configuration chosen in the web
interface as JSON under the vault application home directory
(``~/.talkpipe-vault`` by default). The location is resolved like other
TalkPipe configuration: the ``TALKPIPE_VAULT_HOME`` environment variable, then
a ``VAULT_HOME`` key in ``~/.talkpipe.toml``, then the default.

Model values saved here act as user overrides: ``None``/absent values fall
through to TalkPipe configuration and then to the vault defaults (see
``talkpipe_vault.pipelines.config``).
"""

import json
import logging
import os
from pathlib import Path

from talkpipe.util.config import get_config

logger = logging.getLogger(__name__)

VAULT_HOME_ENV = "TALKPIPE_VAULT_HOME"
# TalkPipe config key (the TALKPIPE_ prefix of the env var maps to this).
VAULT_HOME_CONFIG_KEY = "VAULT_HOME"
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

# Retrieval-filter activation, keyed by resolved vault path: the flags are this
# machine's decision, made separately for each vault, so enabling one vault's
# filter never enables another's. The script itself travels inside the vault
# directory; whether it *runs* is decided here, so a vault copied from elsewhere
# never executes its bundled script until the user enables it.
RETRIEVAL_FILTER_KEY = "retrieval_filters"


def _configured_vault_home() -> str | None:
    """Return a ``VAULT_HOME`` value from TalkPipe config (~/.talkpipe.toml)."""
    try:
        value = get_config().get(VAULT_HOME_CONFIG_KEY)
    except Exception:  # pragma: no cover - defensive; config load is cheap
        return None
    return str(value) if value else None


def get_vault_home() -> Path:
    """Return the vault application home directory (not necessarily existing).

    Resolved like other TalkPipe configuration, highest precedence first: the
    ``TALKPIPE_VAULT_HOME`` environment variable, a ``VAULT_HOME`` key in
    ``~/.talkpipe.toml``, then the ``~/.talkpipe-vault`` default. The env var is
    read directly (not via cached config) so a freshly set value takes effect.
    """
    configured = os.environ.get(VAULT_HOME_ENV) or _configured_vault_home()
    return Path(configured or DEFAULT_VAULT_HOME).expanduser()


def settings_file_path() -> Path:
    """Return the path of the persisted settings file (for user-facing messages)."""
    return get_vault_home() / SETTINGS_FILENAME


def load_settings() -> dict:
    """Load persisted settings, returning an empty structure when unavailable."""
    path = settings_file_path()
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
    path = settings_file_path()
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


def forget_vault(vault_path: str) -> bool:
    """Remove a vault path from the recent-vault list.

    Returns True if the path was present and removed. Does not touch any files
    on disk.
    """
    resolved = str(Path(vault_path).expanduser())
    settings = load_settings()
    recents = settings["recent_vaults"]
    remaining = [p for p in recents if p != resolved]
    if len(remaining) == len(recents):
        return False
    settings["recent_vaults"] = remaining
    save_settings(settings)
    return True


def _resolve_vault_key(vault_path: str) -> str:
    """Normalize a vault path the same way the recent-vault list does."""
    return str(Path(vault_path).expanduser())


def get_retrieval_filter_flags(vault_path: str) -> dict:
    """Return this machine's activation flags for one vault's retrieval filter.

    Flags are stored per vault path, so they say nothing about any other vault.
    Returns {"enabled": bool, "strict": bool}; both default to False for a
    vault with no saved entry — a filter script is inert until enabled here.
    """
    filters = load_settings().get(RETRIEVAL_FILTER_KEY)
    entry = (
        filters.get(_resolve_vault_key(vault_path))
        if isinstance(filters, dict)
        else None
    )
    if not isinstance(entry, dict):
        entry = {}
    return {"enabled": bool(entry.get("enabled")), "strict": bool(entry.get("strict"))}


def set_retrieval_filter_flags(vault_path: str, *, enabled: bool, strict: bool) -> None:
    """Persist the activation flags for one vault's retrieval filter."""
    settings = load_settings()
    filters = settings.get(RETRIEVAL_FILTER_KEY)
    if not isinstance(filters, dict):
        filters = {}
    filters[_resolve_vault_key(vault_path)] = {
        "enabled": bool(enabled),
        "strict": bool(strict),
    }
    settings[RETRIEVAL_FILTER_KEY] = filters
    save_settings(settings)


def clear_retrieval_filter_flags(vault_path: str) -> None:
    """Drop the saved activation flags for a vault's retrieval filter."""
    settings = load_settings()
    filters = settings.get(RETRIEVAL_FILTER_KEY)
    if (
        isinstance(filters, dict)
        and filters.pop(_resolve_vault_key(vault_path), None) is not None
    ):
        settings[RETRIEVAL_FILTER_KEY] = filters
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
