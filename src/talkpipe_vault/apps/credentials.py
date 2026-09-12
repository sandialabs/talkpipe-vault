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

# Canonical env var for the Ollama server URL; other modules should reference
# this rather than re-spelling the literal.
OLLAMA_URL_ENV = "TALKPIPE_OLLAMA_SERVER_URL"


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
    _Field("ollama_server_url", OLLAMA_URL_ENV, False, "Ollama server URL"),
)

_FIELDS_BY_KEY = {field.key: field for field in FIELDS}

# Credentials that hold a server URL rather than a secret. Both interfaces run
# these through normalize_server_url before saving, so a bare "host:port" is
# completed the same way whichever one the user typed it into.
URL_CREDENTIAL_KEYS: tuple[str, ...] = ("openai_base_url", "ollama_server_url")

# Env vars we set from the stored credentials, so we can safely unset only our
# own when a value is cleared (never a variable the environment already had).
_managed_env: set[str] = set()


def store_path() -> Path:
    """Absolute path to the credentials file (whether or not it exists yet)."""
    return user_settings.get_vault_home() / CREDENTIALS_FILENAME


def load() -> dict[str, str]:
    """Load stored credentials, keeping only known non-empty string values."""
    path = store_path()
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
    path = store_path()
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
    apply(data)


def apply(data: dict[str, str] | None = None) -> None:
    """Apply stored credentials to the process environment.

    Sets each configured value's env var, unsets any var we previously set but
    is now cleared, and refreshes TalkPipe's cached config so a changed Ollama
    URL takes effect. Leaves untouched any env var the vault never set.
    ``data`` lets a caller that just loaded/saved the values skip re-reading
    the file.
    """
    global _managed_env
    if data is None:
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


def label_for(key: str) -> str:
    """The form label of a managed credential (the key itself if unknown)."""
    field = _FIELDS_BY_KEY.get(key)
    return field.label if field else key


def normalize_server_url(value: str) -> tuple[str, str]:
    """Tidy a server/base URL typed into a settings form.

    Returns ``(url, note)``. A bare ``host:port`` gets ``http://`` in front
    (and the note says so); a URL with any scheme other than http/https raises
    ``ValueError`` with a message fit for the form, because saving it would
    only fail later, in a probe whose headline blames the server. A blank
    value stays blank (it clears the field).
    """
    typed = value.strip()
    if not typed:
        return "", ""
    scheme, sep, rest = typed.partition("://")
    if not sep:
        # "host:11434" has no "://"; urlparse would take "host" for a scheme.
        cleaned = typed.rstrip("/")
        return f"http://{cleaned}", f"Added http:// in front of {cleaned}."
    if scheme.lower() not in ("http", "https"):
        raise ValueError(
            f"{typed} is not an http(s) URL — enter the server's address "
            "as http://host:port (e.g. http://localhost:11434)."
        )
    if not rest.split("/", 1)[0]:
        raise ValueError(
            f"{typed} has no host — enter the server's address as "
            "http://host:port (e.g. http://localhost:11434)."
        )
    return f"{scheme}://{rest.rstrip('/')}", ""


class UrlCredentialError(ValueError):
    """A URL credential a form should refuse; ``key`` names the field."""

    def __init__(self, key: str, message: str):
        super().__init__(message)
        self.key = key


def normalize_url_changes(
    changes: dict[str, str | None],
) -> tuple[dict[str, str | None], list[str]]:
    """Normalize every URL field in a set of credential changes.

    Returns ``(changes, notes)`` with each URL field passed through
    :func:`normalize_server_url`; ``notes`` collects the human-readable
    outcomes ("Added http:// in front of …") to show alongside the save
    confirmation. Raises :class:`UrlCredentialError` — a ``ValueError`` — with
    a form-ready message prefixed by the offending field's label, for a URL
    that cannot work.

    Both the web Settings page and the terminal interface call this, so the
    same typed value is stored the same way in either one.
    """
    cleaned: dict[str, str | None] = dict(changes)
    notes: list[str] = []
    for key in URL_CREDENTIAL_KEYS:
        value = cleaned.get(key)
        if not value:
            continue
        try:
            url, note = normalize_server_url(str(value))
        except ValueError as exc:
            raise UrlCredentialError(key, f"{label_for(key)}: {exc}") from exc
        cleaned[key] = url
        if note:
            notes.append(note)
    return cleaned, notes


def source_for(env_var: str) -> str:
    """Describe where a managed env var's current value comes from."""
    if env_var in _managed_env:
        return "Vault settings"
    if os.environ.get(env_var):
        return "environment"
    return "unset"


def mask_secret(secret: str, *, short: str = "••••", prefix: str = "••••") -> str:
    """Mask a secret, revealing only the last few characters."""
    if len(secret) <= 8:
        return short
    return f"{prefix}{secret[-4:]}"


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
                "masked": mask_secret(value) if value else "",
                # Only non-secret values are safe to send back to the browser.
                "value": "" if field.secret else value,
                "active": active,
                "source": source_for(field.env_var),
            }
        )
    return rows
