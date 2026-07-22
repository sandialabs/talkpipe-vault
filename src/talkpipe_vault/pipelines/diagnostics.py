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

import importlib.util
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from talkpipe.util.config import get_config
from talkpipe.util.constants import (
    MODEL2VEC_CACHE_DIR,
    MODEL2VEC_REVISION,
    OLLAMA_SERVER_URL,
)

from talkpipe_vault.apps import credentials, user_settings

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_PROBE_TIMEOUT = 3.0

# A functional probe generates an embedding or runs a chat turn, which
# legitimately takes longer than a reachability ping; give it more headroom
# than DEFAULT_PROBE_TIMEOUT (but never less than the caller's own timeout).
FUNCTIONAL_PROBE_TIMEOUT = 20.0

# An explicitly requested first-time model download is far slower again than
# any probe; give it its own budget.
MODEL_DOWNLOAD_TIMEOUT = 300.0

# Short, harmless inputs used when actually exercising a provider.
_PROBE_TEXT = "TalkPipe configuration self-test."
_PROBE_PROMPT = "Reply with the single word: ok."

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
    *,
    vault_selected: bool = False,
    vault_embedding: dict[str, Any] | None = None,
    vault_indexed: bool = True,
    probe: bool = True,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    allow_download: bool = False,
) -> dict[str, Any]:
    """Build the configuration status report for the effective selection.

    Args:
        models: The effective model config (as produced by the app's
            ``_effective_models``); reads ``embedding_source``/``embedding_model``
            and ``chat_source``/``chat_model``.
        vault_selected: When True, add a row comparing the current embedder with
            the one the open vault was indexed with.
        vault_embedding: The open vault's recorded embedding config
            (``source``/``model``/``dimension``), or None for a legacy vault that
            has no record. Only consulted when ``vault_selected`` is True.
        vault_indexed: Whether the open vault contains any indexed documents.
            Distinguishes a new, never-indexed vault (no record is expected)
            from a legacy vault whose index predates the record. Only
            consulted when ``vault_selected`` is True.
        probe: When True, make live connectivity/credential calls. When False,
            report only what can be determined without touching the network.
        timeout: Per-call network timeout, in seconds.
        allow_download: When True, an uncached in-process embedding model
            (model2vec) may be downloaded and exercised, rather than only
            reported as not-yet-downloaded. Used by the explicit Re-test
            action; passive page loads keep this False so opening Settings
            never triggers a download.

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
            allow_download=allow_download,
        ),
        _check_provider(
            "Chat (Ask)",
            models.get("chat_source", ""),
            models.get("chat_model", ""),
            "chat_source",
            probe=probe,
            timeout=timeout,
        ),
    ]
    if vault_selected:
        checks.append(
            _check_embedding_index_match(models, vault_embedding, vault_indexed)
        )
    return {"overall": _rollup(checks), "checks": checks}


def _check_embedding_index_match(
    models: dict[str, Any], recorded: dict[str, Any] | None, vault_indexed: bool = True
) -> dict[str, Any]:
    """Compare the current embedder with the one the open vault was indexed with.

    Semantic search only works when the query is embedded with the same model
    that embedded the documents, so a mismatch is a genuine configuration error.
    No record can mean two very different things: a vault with no documents yet
    has simply not recorded anything (fine — the record is written on first
    index), while a vault that *has* documents but no record is a legacy vault
    that can't be checked, so we say so rather than guess. The current embedder
    is left in place either way.
    """
    current_source = (models.get("embedding_source") or "").strip()
    current_model = (models.get("embedding_model") or "").strip()
    base: dict[str, Any] = {
        "name": "Embedding ↔ index",
        "value": f"{current_source or '—'} / {current_model or '—'}",
    }

    if not recorded:
        if not vault_indexed:
            base["status"] = "ok"
            base["summary"] = (
                "No documents have been indexed into this vault yet, so there "
                "is nothing to compare. The embedding configuration is "
                "recorded when documents are first indexed."
            )
            return base
        base["status"] = "unknown"
        base["summary"] = (
            "This vault has no recorded embedding configuration (it was indexed "
            "before this was tracked). The current embedder is being used as-is; "
            "if search results look wrong, set it to the model the vault was "
            "built with, or re-index."
        )
        return base

    recorded_source = (recorded.get("source") or "").strip()
    recorded_model = (recorded.get("model") or "").strip()
    base["detail"] = f"Indexed with {recorded_source or '—'} / {recorded_model or '—'}"
    dimension = recorded.get("dimension")
    if dimension:
        base["detail"] += f" ({dimension}-dimension vectors)"

    matches = (
        recorded_source.lower() == current_source.lower()
        and recorded_model.lower() == current_model.lower()
    )
    if matches:
        base["status"] = "ok"
        base["summary"] = (
            "The current embedder matches the one this vault was indexed with."
        )
        return base

    base["status"] = "error"
    base["summary"] = (
        f"This vault was indexed with {recorded_source}/{recorded_model}, but the "
        f"current embedder is {current_source}/{current_model}. Semantic search "
        "will be unreliable until they match."
    )
    base["fix"] = (
        f"Set the embedding model back to {recorded_source}/{recorded_model}, or "
        "re-index this vault with the current embedder (Add Documents → Overwrite)."
    )
    return base


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
    allow_download: bool = False,
) -> dict[str, Any]:
    """Check a single provider role (embeddings or chat)."""
    base: dict[str, Any] = {
        "name": f"{label} provider",
        "value": f"{source or '—'} / {model or '—'}",
        "source": _source_provenance(source_setting_key),
    }
    normalized = (source or "").strip().lower()
    role = _role_for_setting(source_setting_key)

    if normalized == "model2vec":
        return _check_model2vec(
            base, model, probe=probe, timeout=timeout, allow_download=allow_download
        )

    if normalized == "eliza":
        base["status"] = "ok"
        base["summary"] = (
            "Eliza is a built-in rule-based responder — no external service or key."
        )
        return base

    if normalized == "ollama":
        return _check_ollama(base, model, role=role, probe=probe, timeout=timeout)

    cloud = {
        "openai": ("OpenAI", "OPENAI_API_KEY"),
        "anthropic": ("Anthropic", "ANTHROPIC_API_KEY"),
    }
    if normalized in cloud:
        display, env_var = cloud[normalized]
        return _check_api_key(
            base,
            display,
            env_var,
            normalized,
            role=role,
            model=model,
            probe=probe,
            timeout=timeout,
        )

    return _check_unrecognized_provider(
        base,
        role,
        source,
        model,
        probe=probe,
        timeout=timeout,
    )


def _role_for_setting(source_setting_key: str) -> str:
    """Map a source setting key to a functional-probe role."""
    return "embedding" if source_setting_key == "embedding_source" else "chat"


def _check_unrecognized_provider(
    base: dict[str, Any],
    role: str,
    source: str,
    model: str,
    *,
    probe: bool,
    timeout: float,
) -> dict[str, Any]:
    """Verify a provider with no bespoke check by actually using it once.

    Diagnostics has no tailored check or setup advice for this source, but
    TalkPipe may still have a registered adapter for it. Rather than give up
    with a bare "can't verify", exercise the provider end to end: embed a short
    string (embeddings) or run a one-shot chat turn (chat). If that succeeds the
    configuration is functionally correct even though we can't offer guidance;
    if it fails we can at least say so and surface the error.

    Sources with no registered adapter (e.g. a typo'd provider name) stay a
    plain warning, since there is nothing to exercise.
    """
    registered_key = _registered_provider_key(role, source)
    if registered_key is None:
        base["status"] = "warn"
        base["summary"] = f"Can't automatically verify provider '{source}'."
        return base

    if not probe:
        base["status"] = "unknown"
        base["summary"] = (
            f"Provider '{source}' has no built-in check and was not exercised "
            "(live probing is disabled)."
        )
        return base

    ok, result = _functional_probe(role, registered_key, model, timeout)
    if ok:
        base["status"] = "ok"
        if role == "embedding":
            base["summary"] = (
                f"No built-in check for '{source}', but a test embedding "
                f"succeeded ({result}-dimension vector), so it is working."
            )
        else:
            base["summary"] = (
                f"No built-in check for '{source}', but a test chat exchange "
                "succeeded, so it is working."
            )
        return base

    base["status"] = "error"
    action = (
        "make a test embedding" if role == "embedding" else "hold a test chat exchange"
    )
    base["summary"] = (
        f"Provider '{source}' is selected, but the attempt to {action} failed."
    )
    base["detail"] = _short_error(result)
    return base


def _registered_provider_key(role: str, source: str) -> str | None:
    """Return the registry key for a source, matched case-insensitively.

    Returns None when TalkPipe has no adapter registered for the source, so the
    caller can fall back to a plain "can't verify" instead of trying to build
    an adapter that does not exist.
    """
    from talkpipe.llm.config import getEmbeddingSources, getPromptSources

    try:
        names = getEmbeddingSources() if role == "embedding" else getPromptSources()
    except Exception:  # pragma: no cover - defensive; registry access is cheap
        return None
    target = (source or "").strip().lower()
    for name in names:
        if name.lower() == target:
            return name
    return None


def _functional_probe(
    role: str, registered_key: str, model: str, timeout: float
) -> tuple[bool, Any]:
    """Actually exercise a provider once, bounded by a timeout.

    Returns ``(True, dimension_or_reply)`` on success or ``(False, error)`` on
    any failure (including timeout). The work runs in a daemon thread so a
    provider that hangs cannot block the Settings page indefinitely.
    """
    bound = max(timeout, FUNCTIONAL_PROBE_TIMEOUT)
    if role == "embedding":
        return _run_bounded(lambda: _embedding_probe(registered_key, model), bound)
    return _run_bounded(lambda: _chat_probe(registered_key, model), bound)


def _embedding_probe(registered_key: str, model: str) -> int:
    """Build the embedding adapter and embed one probe string.

    Returns the vector dimension on success; raises on any failure.
    """
    from talkpipe.llm.config import getEmbeddingAdapter

    adapter = getEmbeddingAdapter(registered_key)(model=model)
    vector = adapter.execute_one(_PROBE_TEXT)
    if vector is None or len(vector) == 0:
        raise ValueError("provider returned an empty embedding vector")
    return len(vector)


def _chat_probe(registered_key: str, model: str) -> str:
    """Build the chat adapter and run one probe turn.

    Returns the (stripped) reply text on success; raises on any failure.
    """
    from talkpipe.llm.config import getPromptAdapter

    adapter = getPromptAdapter(registered_key)(model=model)
    reply = adapter.execute(_PROBE_PROMPT)
    text = "" if reply is None else str(reply).strip()
    if not text:
        raise ValueError("provider returned an empty chat response")
    return text


def _run_bounded(func: Any, timeout: float) -> tuple[bool, Any]:
    """Run ``func()`` in a daemon thread, capping how long we wait.

    On timeout the worker is abandoned rather than joined, so the report still
    returns promptly; the daemon thread cannot keep the process alive.
    """
    import threading

    outcome: dict[str, Any] = {}

    def worker() -> None:
        try:
            outcome["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - surface any failure as text
            outcome["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return False, TimeoutError(f"probe did not finish within {timeout:.0f}s")
    if "error" in outcome:
        return False, outcome["error"]
    return True, outcome.get("value")


def _short_error(exc: Any) -> str:
    """Render an exception as a short, single-line detail string."""
    message = str(exc).strip() or exc.__class__.__name__
    return message[:200]


def _probe_word(role: str) -> str:
    """Human phrase for the action a functional probe performs."""
    return "embedding" if role == "embedding" else "chat exchange"


def _model2vec_finalize(
    base: dict[str, Any], model: str, timeout: float, *, ready_summary: str
) -> dict[str, Any]:
    """Confirm a loadable model2vec model by embedding a test string.

    Called only when the model is known to be loadable without a download (a
    local directory or a ready cache entry), so this never reaches out to the
    network. Success reports ``ready_summary``; failure means the model is
    present but broken (e.g. a corrupt cache or a directory that is not a real
    model), which is an error the user needs to see.
    """
    ok, result = _functional_probe("embedding", "model2vec", model, timeout)
    if ok:
        base["status"] = "ok"
        base["summary"] = ready_summary
        return base
    base["status"] = "error"
    base["summary"] = (
        f"model2vec model '{model}' is present locally but failed to produce a "
        "test embedding."
    )
    base["fix"] = (
        "The local model may be incomplete or corrupt — re-download it, or point "
        "the embedding model at a known-good local model directory."
    )
    base["detail"] = _short_error(result)
    return base


def _sdk_auth_error_type(package: str) -> type | None:
    """Return a cloud SDK's AuthenticationError type, if the SDK is importable."""
    try:
        module = __import__(package)
    except ImportError:
        return None
    error_type = getattr(module, "AuthenticationError", None)
    return error_type if isinstance(error_type, type) else None


_AUTH_KEYWORDS = (
    "authentication",
    "unauthorized",
    "invalid api key",
    "invalid_api_key",
    "invalid x-api-key",
    "incorrect api key",
    "could not resolve authentication",
    "401",
)


def _is_auth_failure(package: str, exc: BaseException) -> bool:
    """Decide whether a failed cloud probe was caused by a bad/rejected key.

    Walks the exception's cause/context chain, matching both the SDK's own
    ``AuthenticationError`` type and common auth-failure phrasing, so a wrapped
    exception is still recognized. Distinguishing this from a transient network
    error is what lets the report say "the key was rejected" (an error the user
    must fix) versus "couldn't validate right now" (a warning).
    """
    sdk_auth = _sdk_auth_error_type(package)
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if sdk_auth is not None and isinstance(current, sdk_auth):
            return True
        text = str(current).lower()
        if any(keyword in text for keyword in _AUTH_KEYWORDS):
            return True
        current = current.__cause__ or current.__context__
    return False


def _check_model2vec(
    base: dict[str, Any],
    model: str,
    *,
    probe: bool,
    timeout: float,
    allow_download: bool = False,
) -> dict[str, Any]:
    """Check that a model2vec model is available locally, then embed a test string.

    model2vec runs in-process but resolves its model through Hugging Face,
    which downloads on first use. Two firewall realities shape this check:

    - An uncached model can only be fetched with outbound access to
      huggingface.co; behind a firewall the download fails, or (worse) hangs
      on connection timeouts.
    - Even a *cached* model, loaded in online mode, makes an etag HEAD request
      that can stall for the full timeout before falling back to the cache.

    So the reliable firewall setup is ``HF_HUB_OFFLINE=1`` plus a pre-populated
    cache (or a local model directory). We first inspect the local cache rather
    than loading the model (so opening Settings never triggers a download), and
    only once the model is known to be loadable — a local directory, or a ready
    cache entry — do we actually make a test embedding to confirm it works.

    A model that simply has not been downloaded yet is not an error — nothing
    has failed — so it reports as a warning. With ``allow_download`` (the
    explicit Re-test action) the model is actually loaded, letting the first
    download happen right there and turning the row green once it works.
    """
    if _is_local_model_dir(model):
        return _model2vec_finalize(
            base,
            model,
            timeout,
            ready_summary=(
                f"Local model directory '{model}' embeds a test string — runs "
                "in-process, no download and no network access."
            ),
        )

    if not probe:
        base["status"] = "unknown"
        base["summary"] = f"model2vec model '{model}' (not checked)."
        return base

    offline = _hf_offline()
    state, detail = _model2vec_cache_state(model)

    if state == "ready":
        if offline:
            summary = (
                f"model2vec model '{model}' loads from the local cache and "
                "embedded a test string; Hugging Face offline mode is on, so it "
                "needs no network access."
            )
            base["detail"] = f"Cached at {detail}"
        else:
            summary = (
                f"model2vec model '{model}' is available locally and ready — "
                "embedded a test string, runs in-process, no server or API key "
                "needed."
            )
            base["detail"] = (
                f"Cached at {detail}. Behind a firewall, start the app with "
                "HF_HUB_OFFLINE=1 so loading never waits on the network."
            )
        return _model2vec_finalize(base, model, timeout, ready_summary=summary)

    if state == "missing_package":
        base["status"] = "error"
        base["summary"] = (
            "model2vec is selected but the model2vec package is not installed."
        )
        base["fix"] = "Install it with: pip install talkpipe[model2vec]"
        return base

    # "absent": the model cannot be loaded from the local cache right now
    # (not cached, or cached without a revision that resolves offline).
    if offline:
        base["status"] = "error"
        base["summary"] = (
            f"Hugging Face offline mode is on, but model2vec model '{model}' "
            "cannot be loaded from the local cache, so indexing and search will "
            "fail. Re-test cannot download it while offline mode is on."
        )
        base["fix"] = (
            "Most reliable behind a firewall: download the model and point the "
            "embedding model at that local directory (loads with no Hugging "
            "Face lookup). Otherwise, fully pre-cache it on a connected machine."
        )
        return base

    if allow_download:
        ok, result = _functional_probe(
            "embedding", "model2vec", model, MODEL_DOWNLOAD_TIMEOUT
        )
        if ok:
            base["status"] = "ok"
            base["summary"] = (
                f"model2vec model '{model}' was downloaded from Hugging Face "
                "and embedded a test string — ready to use, runs in-process."
            )
            return base
        base["status"] = "error"
        base["summary"] = (
            f"model2vec model '{model}' could not be downloaded and loaded."
        )
        base["fix"] = (
            "Check outbound access to huggingface.co and the model name. "
            "Firewalled: point the embedding model at a local model directory, "
            "or pre-cache it and start the app with HF_HUB_OFFLINE=1."
        )
        base["detail"] = _short_error(result)
        return base

    base["status"] = "warn"
    base["summary"] = (
        f"model2vec model '{model}' has not been downloaded yet. It downloads "
        "from Hugging Face automatically on first index or search."
    )
    base["fix"] = (
        "Click Re-test to download it now and confirm it works. Firewalled: "
        "point the embedding model at a local model directory, or pre-cache it "
        "and start the app with HF_HUB_OFFLINE=1."
    )
    return base


def _hf_offline() -> bool:
    """Return True if Hugging Face offline mode is enabled via the environment.

    Only ``HF_HUB_OFFLINE`` governs huggingface_hub's download path (which is
    what model2vec uses). Read fresh from the environment for reporting; note
    that huggingface_hub itself latches this at import time, so it must be set
    before the app starts to actually take effect.
    """
    return os.environ.get("HF_HUB_OFFLINE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _is_local_model_dir(model: str) -> bool:
    """Return True if the model name refers to an existing local directory."""
    try:
        return Path(model).expanduser().is_dir()
    except OSError:  # pragma: no cover - defensive against odd path values
        return False


def _model2vec_cache_state(model: str) -> tuple[str, str | None]:
    """Return the local availability of a model2vec model without downloading.

    Returns one of ``("ready", path)``, ``("absent", None)``, or
    ``("missing_package", None)``. Honors the same cache dir / revision config
    that the model2vec adapter uses, so it looks where the model would load
    from.
    """
    if importlib.util.find_spec("model2vec") is None:
        return "missing_package", None
    try:
        import huggingface_hub
    except ImportError:
        return "missing_package", None

    config = get_config()
    cache_dir = config.get(MODEL2VEC_CACHE_DIR)
    revision = config.get(MODEL2VEC_REVISION)
    try:
        # local_files_only forces an offline lookup: it returns the snapshot
        # path if fully cached and raises otherwise — never hits the network.
        path = huggingface_hub.snapshot_download(
            repo_id=model,
            revision=revision,
            cache_dir=str(cache_dir) if cache_dir else None,
            local_files_only=True,
        )
        return "ready", path
    except Exception:  # noqa: BLE001 - any offline failure means "not cached"
        return "absent", None


def _check_ollama(
    base: dict[str, Any], model: str, *, role: str, probe: bool, timeout: float
) -> dict[str, Any]:
    """Check the configured Ollama server is reachable, has the model, and works.

    Reachability and model-presence come first so their specific fixes (set the
    server URL / ``ollama pull``) are reported even when nothing works yet. Once
    both pass, a real embedding or chat turn confirms the server URL and model
    actually function for the role they are configured for.
    """
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
            "Start Ollama, set the Ollama server URL under Connections & "
            "credentials below, or point TALKPIPE_OLLAMA_SERVER_URL at your "
            "server (e.g. http://your-ollama-host:11434)."
        )
        return base

    if not _model_present(model, names):
        base["status"] = "error"
        base["summary"] = (
            f"Ollama is reachable at {url}, but model '{model}' is not pulled."
        )
        base["fix"] = f"Run: ollama pull {model}"
        return base

    ok, result = _functional_probe(role, "ollama", model, timeout)
    if ok:
        base["status"] = "ok"
        base["summary"] = (
            f"Ollama at {url} completed a test {_probe_word(role)} with model "
            f"'{model}'."
        )
        return base

    base["status"] = "error"
    base["summary"] = (
        f"Ollama at {url} has model '{model}', but a test {_probe_word(role)} "
        "failed."
    )
    base["detail"] = f"{base['detail']} — {_short_error(result)}"
    return base


def _check_api_key(
    base: dict[str, Any],
    display: str,
    env_var: str,
    package: str,
    *,
    role: str,
    model: str,
    probe: bool,
    timeout: float,
) -> dict[str, Any]:
    """Check a cloud provider's key is set and actually works for its model.

    The vendor SDKs read these keys directly from the environment, not from
    TalkPipe config, so the presence check targets the environment variable.
    When probing is enabled, the key is validated by actually making a test
    embedding or chat exchange with the configured model — proving not just that
    the key is present, but that it is accepted and grants access to that model.
    """
    key = os.environ.get(env_var)
    if not key:
        base["status"] = "error"
        base["summary"] = f"{display} is selected but no {display} API key is set."
        base["fix"] = (
            f"Enter your {display} API key under Connections & credentials below, "
            f"or set {env_var} in the environment."
        )
        return base

    base["detail"] = (
        f"{env_var} is set ({_mask(key)}); from {credentials.source_for(env_var)}."
    )

    if not probe:
        base["status"] = "ok"
        base["summary"] = f"{env_var} is set (not validated)."
        return base

    ok, result = _functional_probe(role, package, model, timeout)
    if ok:
        base["status"] = "ok"
        base["summary"] = (
            f"{display} key works — a test {_probe_word(role)} with model "
            f"'{model}' succeeded."
        )
        return base

    if _is_auth_failure(package, result):
        base["status"] = "error"
        base["summary"] = f"{env_var} is set but {display} rejected it."
        base["fix"] = f"Check that {env_var} is a valid, active {display} API key."
        return base

    # Present but unverifiable (network error, model access, unexpected SDK, …).
    base["status"] = "warn"
    base["summary"] = (
        f"{env_var} is set, but a test {_probe_word(role)} could not be completed."
    )
    base["detail"] = f"{base['detail']} — {_short_error(result)}"
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
    env_url = os.environ.get(credentials.OLLAMA_URL_ENV)
    if env_url:
        if credentials.source_for(credentials.OLLAMA_URL_ENV) == "Vault settings":
            return env_url.rstrip("/"), "Vault settings"
        return env_url.rstrip("/"), f"{credentials.OLLAMA_URL_ENV} env var"
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
    scheme = urllib.parse.urlparse(url).scheme
    if scheme not in ("http", "https"):
        return None, f"unsupported URL scheme {scheme!r} (expected http or https)"
    try:
        request = urllib.request.Request(f"{url}/api/tags", method="GET")
        with urllib.request.urlopen(  # nosec B310 - scheme validated above
            request, timeout=timeout
        ) as response:
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


def _mask(secret: str) -> str:
    """Mask a secret, revealing only the last few characters."""
    return credentials.mask_secret(secret, short="****", prefix="…")
