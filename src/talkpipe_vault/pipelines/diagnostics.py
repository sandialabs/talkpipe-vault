"""Configuration diagnostics for the vault web application.

Produces a selection-aware health report for the providers a vault is
actually configured to use. The goal is that a user who has never heard of
an environment variable can open the Settings page, read a short list of
checks, and see exactly what is configured, *where each value came from*, and
what to do about anything that is broken.

Only the providers referenced by the current embedding/chat selection are
tested: the zero-config default (model2vec embeddings + local Ollama chat)
requires no credentials, so we never nag about ``OPENAI_API_KEY`` unless
something is actually set to OpenAI.

The report is a plain JSON-serializable dict so it can be returned straight
from a FastAPI endpoint and rendered by the Settings page.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from talkpipe.util.config import get_config
from talkpipe.util.constants import OLLAMA_SERVER_URL

from talkpipe_vault.apps import user_settings

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_PROBE_TIMEOUT = 3.0

# Status values, worst first. Used both as row status and for the roll-up.
_STATUS_ORDER = ("error", "warn", "unknown", "ok")

# Config-key aliases mirroring pipelines/config.py, so provenance detection
# agrees with how values are actually resolved there.
_SOURCE_ALIASES = {
    "embedding_source": [
        "embedding_source",
        "EMBEDDING_SOURCE",
        "default_embedding_model_source",
    ],
    "chat_source": ["chat_source", "CHAT_SOURCE", "default_model_source"],
}


def collect_config_status(
    models: dict[str, Any],
    vault_path: str | None = None,
    *,
    probe: bool = True,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> dict[str, Any]:
    """Build the configuration status report for the effective selection.

    Args:
        models: The effective model config (as produced by the app's
            ``_effective_models``); reads ``embedding_source``/``embedding_model``
            and ``chat_source``/``chat_model``.
        vault_path: Currently selected vault path, or empty/None if none.
        probe: When True, make live connectivity/credential calls. When False,
            report only what can be determined without touching the network.
        timeout: Per-call network timeout, in seconds.

    Returns:
        Dict with ``overall`` (worst status) and ``checks`` (list of rows).
    """
    checks = [
        _check_provider(
            "Embeddings",
            models.get("embedding_source", ""),
            models.get("embedding_model", ""),
            "embedding_source",
            probe=probe,
            timeout=timeout,
        ),
        _check_provider(
            "Chat (Ask)",
            models.get("chat_source", ""),
            models.get("chat_model", ""),
            "chat_source",
            probe=probe,
            timeout=timeout,
        ),
        _check_vault(vault_path),
    ]
    return {"overall": _rollup(checks), "checks": checks}


def _rollup(checks: list[dict[str, Any]]) -> str:
    """Return the worst status among the checks."""
    present = {check.get("status", "ok") for check in checks}
    for status in _STATUS_ORDER:
        if status in present:
            return status
    return "ok"


# --------------------------------------------------------------------------
# Provider checks
# --------------------------------------------------------------------------


def _check_provider(
    label: str,
    source: str,
    model: str,
    source_setting_key: str,
    *,
    probe: bool,
    timeout: float,
) -> dict[str, Any]:
    """Check a single provider role (embeddings or chat)."""
    base: dict[str, Any] = {
        "name": f"{label} provider",
        "value": f"{source or '—'} / {model or '—'}",
        "source": _source_provenance(source_setting_key),
    }
    normalized = (source or "").strip().lower()

    if normalized == "model2vec":
        base["status"] = "ok"
        base["summary"] = (
            f"model2vec runs in-process — no server or API key needed. "
            f"Model '{model}' downloads from Hugging Face on first use."
        )
        return base

    if normalized == "eliza":
        base["status"] = "ok"
        base["summary"] = (
            "Eliza is a built-in rule-based responder — no external service or key."
        )
        return base

    if normalized == "ollama":
        return _check_ollama(base, model, probe=probe, timeout=timeout)

    if normalized == "openai":
        return _check_api_key(
            base, "OpenAI", "OPENAI_API_KEY", "openai", probe=probe, timeout=timeout
        )

    if normalized == "anthropic":
        return _check_api_key(
            base,
            "Anthropic",
            "ANTHROPIC_API_KEY",
            "anthropic",
            probe=probe,
            timeout=timeout,
        )

    base["status"] = "warn"
    base["summary"] = f"Can't automatically verify provider '{source}'."
    return base


def _check_ollama(
    base: dict[str, Any], model: str, *, probe: bool, timeout: float
) -> dict[str, Any]:
    """Check that the configured Ollama server is reachable and has the model."""
    url, url_source = _ollama_url_and_source()
    base["detail"] = f"Ollama server: {url} ({url_source})"

    if not probe:
        base["status"] = "unknown"
        base["summary"] = f"Ollama configured at {url} (not tested)."
        return base

    names, error = _ollama_tags(url, timeout)
    if error is not None:
        base["status"] = "error"
        base["summary"] = f"Can't reach Ollama at {url}."
        base["detail"] = f"{base['detail']} — {error}"
        base["fix"] = (
            "Start Ollama, or point TALKPIPE_OLLAMA_SERVER_URL at your server "
            "(e.g. http://your-ollama-host:11434)."
        )
        return base

    if _model_present(model, names):
        base["status"] = "ok"
        base["summary"] = f"Ollama reachable at {url}; model '{model}' is available."
        return base

    base["status"] = "error"
    base["summary"] = (
        f"Ollama is reachable at {url}, but model '{model}' is not pulled."
    )
    base["fix"] = f"Run: ollama pull {model}"
    return base


def _check_api_key(
    base: dict[str, Any],
    display: str,
    env_var: str,
    package: str,
    *,
    probe: bool,
    timeout: float,
) -> dict[str, Any]:
    """Check that a cloud provider's API key is set (and optionally valid).

    The vendor SDKs read these keys directly from the environment, not from
    TalkPipe config, so this checks the environment variable specifically.
    """
    key = os.environ.get(env_var)
    if not key:
        base["status"] = "error"
        base["summary"] = f"{display} is selected but {env_var} is not set."
        base["fix"] = (
            f"Set {env_var} in your environment — the {display} SDK reads it "
            "directly (not TALKPIPE_* keys)."
        )
        return base

    base["detail"] = f"{env_var} is set ({_mask(key)})."

    if not probe:
        base["status"] = "ok"
        base["summary"] = f"{env_var} is set (not validated)."
        return base

    result = _probe_api_key(package, timeout)
    if result is None:
        base["status"] = "ok"
        base["summary"] = f"{display} key is set and validated."
        return base
    if result == "auth":
        base["status"] = "error"
        base["summary"] = f"{env_var} is set but {display} rejected it."
        base["fix"] = f"Check that {env_var} is a valid, active {display} API key."
        return base

    # Present but unverifiable (network error, unexpected SDK shape, etc.).
    base["status"] = "warn"
    base["summary"] = f"{env_var} is set, but the key could not be validated."
    base["detail"] = f"{base['detail']} — {result}"
    return base


# --------------------------------------------------------------------------
# Vault check
# --------------------------------------------------------------------------


def _check_vault(vault_path: str | None) -> dict[str, Any]:
    """Check that a vault is selected, present, and writable."""
    base: dict[str, Any] = {"name": "Vault", "value": vault_path or "—"}
    if not vault_path:
        base["status"] = "warn"
        base["summary"] = "No vault selected."
        base["fix"] = "Choose or create a vault on the Vaults page."
        return base

    path = Path(vault_path)
    if not path.exists():
        base["status"] = "error"
        base["summary"] = f"Vault path does not exist: {vault_path}."
        base["fix"] = "Create the vault, or pick an existing one on the Vaults page."
        return base

    if not os.access(vault_path, os.W_OK):
        base["status"] = "warn"
        base["summary"] = f"Vault path is not writable: {vault_path}."
        base["fix"] = "Indexing needs write access — check the folder's permissions."
        return base

    base["status"] = "ok"
    base["summary"] = f"Vault is ready at {vault_path}."
    return base


# --------------------------------------------------------------------------
# Provenance helpers
# --------------------------------------------------------------------------


def _source_provenance(source_setting_key: str) -> str:
    """Describe where a source selection came from (highest precedence wins).

    Mirrors the precedence in pipelines/config.py: Settings page overrides,
    then TalkPipe config (file or env), then the built-in vault default.
    """
    if source_setting_key in user_settings.get_model_overrides():
        return "Settings page"
    aliases = _SOURCE_ALIASES.get(source_setting_key, [source_setting_key])
    if _config_has(aliases):
        return "TalkPipe config (~/.talkpipe.toml or TALKPIPE_* env)"
    return "vault default"


def _config_has(aliases: list[str]) -> bool:
    """Return True if any alias key is present in TalkPipe config."""
    try:
        config = get_config()
    except Exception:  # pragma: no cover - defensive; config load is cheap
        return False
    vault_section = config.get("vault", {}) or {}
    for key in aliases:
        if vault_section.get(key) is not None or config.get(key) is not None:
            return True
    return False


def _ollama_url_and_source() -> tuple[str, str]:
    """Resolve the Ollama server URL and a human label for where it came from."""
    env_url = os.environ.get("TALKPIPE_OLLAMA_SERVER_URL")
    if env_url:
        return env_url.rstrip("/"), "TALKPIPE_OLLAMA_SERVER_URL env var"
    try:
        configured = get_config().get(OLLAMA_SERVER_URL)
    except Exception:  # pragma: no cover - defensive
        configured = None
    if configured:
        return str(configured).rstrip("/"), "OLLAMA_SERVER_URL in ~/.talkpipe.toml"
    return DEFAULT_OLLAMA_URL, "default (local)"


# --------------------------------------------------------------------------
# Low-level probes
# --------------------------------------------------------------------------


def _ollama_tags(url: str, timeout: float) -> tuple[list[str] | None, str | None]:
    """Return (model names, None) on success or (None, error message) on failure."""
    try:
        request = urllib.request.Request(f"{url}/api/tags", method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.URLError as exc:
        return None, str(getattr(exc, "reason", exc))
    except (OSError, ValueError) as exc:
        return None, str(exc)
    models = payload.get("models", []) if isinstance(payload, dict) else []
    return [str(entry.get("name", "")) for entry in models], None


def _model_present(model: str, names: list[str]) -> bool:
    """Match a configured model name against Ollama's tag list.

    Ollama reports tagged names like ``mistral-small:latest``; a user may
    configure either ``mistral-small`` or a specific tag. Match on the exact
    name or the base name before the tag separator.
    """
    if not model:
        return False
    wanted_base = model.split(":", 1)[0]
    for name in names:
        if name == model or name.split(":", 1)[0] == wanted_base:
            return True
    return False


def _probe_api_key(package: str, timeout: float) -> str | None:
    """Validate a cloud key with a cheap authenticated call.

    Returns None when the key works, ``"auth"`` when the provider rejects it,
    or a short error string when the key is present but validation could not
    be completed (network error, unexpected SDK, etc.).
    """
    try:
        module = __import__(package)
    except ImportError:
        return f"{package} package is not installed"

    try:
        if package == "openai":
            client = module.OpenAI(timeout=timeout)
            client.models.list()
        elif package == "anthropic":
            client = module.Anthropic()
            client.models.list(timeout=timeout)
        else:  # pragma: no cover - only known cloud providers reach here
            return "unsupported provider"
        return None
    except Exception as exc:  # noqa: BLE001 - classify by SDK exception name
        auth_error = getattr(module, "AuthenticationError", None)
        if auth_error is not None and isinstance(exc, auth_error):
            return "auth"
        message = str(exc).strip() or exc.__class__.__name__
        return message[:160]


def _mask(secret: str) -> str:
    """Mask a secret, revealing only the last few characters."""
    if len(secret) <= 8:
        return "****"
    return f"…{secret[-4:]}"
