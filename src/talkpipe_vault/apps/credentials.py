"""Persistent, vault-scoped provider credentials.

Secrets and connection settings a user enters on the Settings page (API keys,
the OpenAI base URL, the Ollama server URL) are stored in ``credentials.json``
under the vault home (``$TALKPIPE_VAULT_HOME``) with owner-only permissions,
and applied into the *process* environment at startup and whenever they change.

Applying to ``os.environ`` — rather than the user's shell or ``~/.talkpipe.toml``
— is what scopes these to the vault app: the OpenAI and Anthropic SDKs read
their key and base URL directly from the environment, and TalkPipe reads the
Ollama server URL from its own config (which we refresh from the same env var).
Nothing is written outside the vault home, so other TalkPipe usage on the
machine is unaffected.

Precedence: a stored value overrides a pre-existing environment variable, so
what a user types in the UI wins. We only ever unset variables we set
ourselves, so a deployment that provides credentials purely through the
environment (e.g. the container ``.env``) keeps working as long as the user
leaves the corresponding field blank.
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from talkpipe.util.config import reset_config

from talkpipe_vault.apps import user_settings

logger = logging.getLogger(__name__)

CREDENTIALS_FILENAME = "credentials.json"


@dataclass(frozen=True)
class _Field:
    """A managed credential: its storage key, target env var, and metadata."""

    key: str
    env_var: str
    secret: bool
    label: str


# Order here is the order rendered on the Settings page.
FIELDS: tuple[_Field, ...] = (
    _Field("openai_api_key", "OPENAI_API_KEY", True, "OpenAI API key"),
    _Field("openai_base_url", "OPENAI_BASE_URL", False, "OpenAI base URL"),
    _Field("anthropic_api_key", "ANTHROPIC_API_KEY", True, "Anthropic API key"),
    _Field(
        "ollama_server_url", "TALKPIPE_OLLAMA_SERVER_URL", False, "Ollama server URL"
    ),
)

_FIELDS_BY_KEY = {field.key: field for field in FIELDS}

# Env vars we set from the stored credentials, so we can safely unset only our
# own when a value is cleared (never a variable the environment already had).
_managed_env: set[str] = set()


def _path() -> Path:
    return user_settings.get_vault_home() / CREDENTIALS_FILENAME


def store_path() -> Path:
    """Absolute path to the credentials file (whether or not it exists yet)."""
    return _path()


def load() -> dict[str, str]:
    """Load stored credentials, keeping only known non-empty string values."""
    path = _path()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("credentials root is not an object")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable credentials file %s: %s", path, exc)
        return {}

    result: dict[str, str] = {}
    for field in FIELDS:
        value = data.get(field.key)
        if isinstance(value, str) and value.strip():
            result[field.key] = value.strip()
    return result


def _write(values: dict[str, str]) -> None:
    """Persist credentials as JSON with owner-only (0600) permissions."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    # Create with 0600 from the start so the secret is never briefly world-readable.
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(values, handle, indent=2)
    os.replace(tmp_path, path)


def set_values(changes: dict[str, str | None]) -> None:
    """Merge credential changes, persist them, and apply to the environment.

    ``changes`` maps field keys to values. A blank or ``None`` value clears
    that field; fields absent from ``changes`` are left untouched. Unknown
    keys are ignored.
    """
    data = load()
    for key, value in changes.items():
        if key not in _FIELDS_BY_KEY:
            continue
        cleaned = (value or "").strip()
        if cleaned:
            data[key] = cleaned
        else:
            data.pop(key, None)
    _write(data)
    apply()


def apply() -> None:
    """Apply stored credentials to the process environment.

    Sets each configured value's env var, unsets any var we previously set but
    is now cleared, and refreshes TalkPipe's cached config so a changed Ollama
    URL takes effect. Leaves untouched any env var the vault never set.
    """
    global _managed_env
    data = load()
    now_managed: set[str] = set()
    for field in FIELDS:
        value = data.get(field.key)
        if value:
            os.environ[field.env_var] = value
            now_managed.add(field.env_var)
        elif field.env_var in _managed_env:
            os.environ.pop(field.env_var, None)
    _managed_env = now_managed
    # TalkPipe caches config (including the Ollama URL) on first read; refresh
    # so a newly applied TALKPIPE_OLLAMA_SERVER_URL is picked up.
    reset_config()


def source_for(env_var: str) -> str:
    """Describe where a managed env var's current value comes from."""
    if env_var in _managed_env:
        return "Vault settings"
    if os.environ.get(env_var):
        return "environment"
    return "unset"


def _mask(secret: str) -> str:
    """Mask a secret, revealing only the last few characters."""
    if len(secret) <= 8:
        return "••••"
    return f"••••{secret[-4:]}"


def describe() -> list[dict[str, object]]:
    """Return per-field state for rendering the Settings form.

    Secret values are never returned verbatim — only whether one is saved and
    a masked hint. Non-secret values (URLs) are returned so the field can be
    pre-filled.
    """
    data = load()
    rows: list[dict[str, object]] = []
    for field in FIELDS:
        value = data.get(field.key, "")
        # A non-secret value may be active from the environment (e.g. a
        # container's TALKPIPE_OLLAMA_SERVER_URL) without being stored here.
        # Surface it so the field isn't misleadingly blank. Secrets are never
        # revealed, so we only report whether one is active, not its value.
        active = "" if field.secret else os.environ.get(field.env_var, "")
        rows.append(
            {
                "key": field.key,
                "label": field.label,
                "secret": field.secret,
                "env_var": field.env_var,
                "present": bool(value),
                "masked": _mask(value) if value else "",
                # Only non-secret values are safe to send back to the browser.
                "value": "" if field.secret else value,
                "active": active,
                "source": source_for(field.env_var),
            }
        )
    return rows
