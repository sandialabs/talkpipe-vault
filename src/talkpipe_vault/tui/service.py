"""In-process facade over the vault application for the terminal interface.

The web routes in ``apps/query.py`` are thin wrappers around module-level
helpers (``init_pipelines``, ``_refresh_pipelines``, ``start_index_job`` …)
operating on the ``_state`` singleton. The TUI drives exactly those helpers
through this class, so both interfaces share one implementation and the web
application itself is untouched. Every method here is synchronous and may
block (embedding, LLM calls, index builds); the TUI calls them from worker
threads.

Return values are plain dicts/lists so the UI layer never sees pipeline
objects or exceptions: each operation reports ``ok``/``error``/``message``
the way the web pages report flash messages.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from talkpipe.llm.config import getEmbeddingSources, getPromptSources

from talkpipe_vault.apps import access_control, credentials, query, user_settings
from talkpipe_vault.pipelines import retrieval_filter, vault_metadata
from talkpipe_vault.pipelines.config import ensure_supported_vault_layout


def _ok(message: str = "", **extra: Any) -> dict[str, Any]:
    return {"ok": True, "message": message, "error": "", **extra}


def _fail(error: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "message": "", "error": error, **extra}


class VaultService:
    """Everything the TUI can do, expressed as plain-data operations."""

    def __init__(self, *, show_source_paths: bool = False) -> None:
        self.state = query._state
        self.state.show_source_paths = show_source_paths

    # -- startup ----------------------------------------------------------------

    def startup(self, vault_path: str = "", *, resume: bool = False) -> dict[str, Any]:
        """Apply saved credentials/settings and open the initial vault.

        Mirrors ``run_app``: a vault that cannot be opened degrades to "no
        vault" with a warning rather than aborting, so Settings and its
        configuration status stay reachable.
        """
        problems = access_control.startup_errors()
        if problems:
            return _fail(" ".join(problems))
        credentials.apply()
        query.load_saved_model_overrides()
        chosen = ""
        resumed = False
        if resume:
            chosen = self.most_recent_usable_vault()
            resumed = bool(chosen)
        if not chosen and vault_path:
            chosen = str(Path(vault_path).expanduser())
        if not chosen:
            return _ok("No vault selected yet — open or create one on the Vault tab.")
        if not access_control.vault_path_allowed(chosen):
            return _fail(
                f"Vault path {chosen} is outside {access_control.VAULT_ROOT_ENV} "
                f"({access_control.vault_root()})."
            )
        try:
            Path(chosen).mkdir(parents=True, exist_ok=True)
            query.init_pipelines(chosen)
        except (OSError, ValueError) as exc:
            return _fail(f"Error opening vault at {chosen}: {exc}")
        except Exception as exc:
            self._clear_vault()
            return _fail(
                f"Could not open vault at {chosen}: {exc} Starting without a "
                "vault — check Settings → Configuration status, then reopen "
                "the vault from the Vault tab."
            )
        user_settings.remember_vault(chosen)
        note = " (most recently used)" if resumed else ""
        return _ok(f"Opened vault {chosen}{note}.", vault_path=chosen)

    @staticmethod
    def most_recent_usable_vault() -> str:
        for candidate in user_settings.get_recent_vaults():
            path = Path(candidate).expanduser()
            if not path.is_dir() or not access_control.vault_path_allowed(str(path)):
                continue
            try:
                ensure_supported_vault_layout(str(path))
            except ValueError:
                continue
            return str(path)
        return ""

    def _clear_vault(self) -> None:
        state = self.state
        state.vault_path = ""
        state.search_pipeline = None
        state.chat_pipeline = None
        state.keyword_chat_pipeline = None
        state.keyword_search_pipeline = None
        state.result_filter_active = False
        state.filtered_search_pipeline = None
        state.filtered_keyword_search_pipeline = None
        state.shingled_chunks_count = 0
        state.keyword_search_enabled = False

    # -- status -------------------------------------------------------------------

    @property
    def vault_path(self) -> str:
        return self.state.vault_path

    def status(self) -> dict[str, Any]:
        """Header/status-bar facts (cheap; no pipeline work)."""
        state = self.state
        return {
            "vault_path": state.vault_path,
            "chunks": state.shingled_chunks_count,
            "keyword_search_enabled": state.keyword_search_enabled,
            "filter_active": state.result_filter_active,
            "filter_error": state.result_filter_error,
            "show_source_paths": state.show_source_paths,
            "models": query._effective_models(state),
        }

    def refresh(self) -> dict[str, Any]:
        """Force-rebuild pipelines and recount documents (the web Refresh)."""
        if not self.state.vault_path:
            return _fail("No vault is open.")
        try:
            query._refresh_pipelines(force=True)
            query._update_document_counts(self.state.vault_path)
        except Exception as exc:
            return _fail(f"Refresh failed: {exc}")
        return _ok("Pipelines refreshed.")

    # -- vaults -------------------------------------------------------------------

    def recent_vaults(self) -> list[dict[str, Any]]:
        current = (
            os.path.realpath(self.state.vault_path) if self.state.vault_path else ""
        )
        return [
            {
                "path": path,
                "exists": Path(path).expanduser().is_dir(),
                "open": bool(current)
                and current == os.path.realpath(os.path.expanduser(path)),
            }
            for path in user_settings.get_recent_vaults()
        ]

    def vault_example(self) -> str:
        root = access_control.vault_root()
        return str(root / "my-vault") if root else "~/my-vault"

    def suggest_vault_path(self, source: str) -> str:
        return query.suggest_vault_path(source) if source.strip() else ""

    def open_vault(
        self, raw_path: str, *, confirm_non_vault: bool = False
    ) -> dict[str, Any]:
        """Open an existing vault or create one (same rules as ``/vaults/open``).

        Returns ``needs_confirm`` with ``confirm_path``/``entry_count`` when
        the target is a non-empty folder that holds no vault data.
        """
        vault_path, placement_note, error = query._resolve_vault_request(raw_path)
        if vault_path is None:
            return _fail(error)
        created = not vault_path.is_dir()
        entries = query._existing_non_vault_entries(vault_path)
        if entries > 0 and not confirm_non_vault:
            return _fail(
                f"{vault_path} already contains {entries} item(s) that are not "
                "vault data.",
                needs_confirm=True,
                confirm_path=str(vault_path),
                entry_count=entries,
            )
        error = query._activate_vault(vault_path)
        if error:
            return _fail(error)
        if created:
            message = (
                f"Created new vault at {vault_path}. Add documents to make it "
                f"searchable.{placement_note}"
            )
        elif entries > 0:
            message = (
                f"Started a new vault at {vault_path}. This folder already holds "
                "files that are not vault data; index files will be created "
                f"alongside them.{placement_note}"
            )
        else:
            message = f"Opened vault at {vault_path}.{placement_note}"
        return _ok(message, vault_path=str(vault_path), created=created)

    def delete_vault(self, raw_path: str) -> dict[str, Any]:
        """Forget a recent vault and delete its folder (``/vaults/delete``)."""
        resolved = os.path.expanduser(raw_path.strip())
        if not resolved:
            return _fail("No vault specified.")
        if resolved not in user_settings.get_recent_vaults():
            return _fail("That vault is not in the recent list.")
        state = self.state
        if state.vault_path and os.path.realpath(state.vault_path) == os.path.realpath(
            resolved
        ):
            return _fail(
                "Cannot delete the vault that is currently open. Open a different "
                "vault first."
            )
        real_path = access_control.confine_vault(resolved)
        if real_path is None:
            user_settings.forget_vault(resolved)
            return _ok(
                f"Removed {resolved} from the list. It is outside "
                f"{access_control.vault_root()}, so its files were left untouched."
            )
        if query._is_dangerous_delete_target(real_path):
            return _fail(
                f"Refusing to delete {resolved}: path is too broad to be a vault."
            )
        existed = real_path.is_dir()
        try:
            if existed:
                shutil.rmtree(real_path)
        except OSError as exc:
            return _fail(f"Failed to delete {resolved}: {exc}")
        user_settings.forget_vault(resolved)
        if existed:
            return _ok(f"Deleted vault {resolved} and removed it from the list.")
        return _ok(
            f"Removed {resolved} from the list (its folder was already gone from disk)."
        )

    def list_directories(self, path: str = "") -> dict[str, Any]:
        """Folder-picker listing, identical to ``GET /api/directories``."""
        response = asyncio.run(query.list_directories(path=path))
        payload: dict[str, Any] = json.loads(bytes(response.body))
        payload["ok"] = response.status_code < 400
        return payload

    # -- indexing -----------------------------------------------------------------

    def start_indexing(
        self,
        source: str,
        vault: str = "",
        *,
        overwrite: bool = False,
        confirm_non_vault: bool = False,
    ) -> dict[str, Any]:
        """Validate and start the background indexing job (``/documents/index``)."""
        source = source.strip()
        if not source:
            if vault.strip():
                return self.open_vault(vault, confirm_non_vault=confirm_non_vault)
            return _fail("Enter a folder or glob pattern to index.")
        pattern = query._resolve_source_pattern(source)
        doc_roots = access_control.document_roots()
        confined_prefix = access_control.confine(
            query._nonglob_prefix(pattern), doc_roots
        )
        if confined_prefix is None:
            return _fail(
                "Indexing on this machine is limited to documents under "
                f"{access_control.describe(doc_roots)}."
            )
        if not confined_prefix.exists():
            return _fail(
                f"'{source}' matched no files. Check the folder path or glob pattern."
            )
        vault_note = ""
        if vault.strip():
            vault_path, placement_note, error = query._resolve_vault_request(vault)
            if vault_path is None:
                return _fail(error)
            if os.path.realpath(vault_path) == os.path.realpath(
                query._nonglob_prefix(pattern)
            ):
                return _fail(
                    f"The vault cannot be the folder being indexed ({vault_path}). "
                    "The vault holds the search index; choose a different, empty "
                    "folder for it."
                )
            already_open = bool(self.state.vault_path) and os.path.realpath(
                self.state.vault_path
            ) == os.path.realpath(vault_path)
            if not already_open:
                created = not vault_path.is_dir()
                entries = query._existing_non_vault_entries(vault_path)
                if entries > 0 and not confirm_non_vault:
                    return _fail(
                        f"{vault_path} already contains {entries} item(s) that are "
                        "not vault data.",
                        needs_confirm=True,
                        confirm_path=str(vault_path),
                        entry_count=entries,
                    )
                error = query._activate_vault(vault_path)
                if error:
                    return _fail(error)
                vault_note = (
                    f"Created vault {vault_path}.{placement_note} "
                    if created
                    else f"Indexing into vault {vault_path}.{placement_note} "
                )
        elif not self.state.vault_path:
            return _fail("Choose a vault to index these documents into.")

        models = query._effective_models(self.state)
        started = query.start_index_job(
            vault_path=self.state.vault_path,
            source=source,
            pattern=pattern,
            embedding_model=models["embedding_model"],
            embedding_source=models["embedding_source"],
            chunk_size=models["chunk_size"],
            shingle_size=models["shingle_size"],
            shingle_overlap=models["shingle_overlap"],
            overwrite=overwrite,
        )
        if not started:
            return _fail(
                "An indexing run is already in progress; wait for it to finish."
            )
        return _ok(
            f"{vault_note}Indexing started. Embedding with "
            f"{models['embedding_source']}/{models['embedding_model']}."
        )

    @staticmethod
    def index_status() -> dict[str, Any]:
        return query._index_job_snapshot()

    def start_fulltext_index(self) -> dict[str, Any]:
        if not self.state.vault_path:
            return _fail("No vault is open.")
        if not query.start_fulltext_index_job(self.state.vault_path):
            return _fail(
                "A full-text index build is already in progress; wait for it to finish."
            )
        return _ok("Building the full-text index.")

    @staticmethod
    def fulltext_status() -> dict[str, Any]:
        return query._fulltext_index_job_snapshot()

    # -- search -------------------------------------------------------------------

    def semantic_search(
        self, text: str, *, apply_filter: bool = False
    ) -> dict[str, Any]:
        state = self.state
        if not state.vault_path:
            return _fail("Open a vault first.")
        if not text.strip():
            return _ok(results=[], note="")
        if not state.search_pipeline:
            return _fail("The search pipeline is not available; check Settings.")
        try:
            query._refresh_pipelines()
            query._update_document_counts(state.vault_path)
            note = ""
            if apply_filter and state.filtered_search_pipeline:
                raw, filter_error = state.filtered_search_pipeline(text)
                results = query._process_semantic_results(raw)
                note = query._filter_outcome_note(len(results), filter_error)
            else:
                results = query._process_semantic_results(state.search_pipeline(text))
        except Exception as exc:
            return _fail(str(exc))
        return _ok(results=results, note=note)

    def keyword_search(
        self, text: str, *, apply_filter: bool = False
    ) -> dict[str, Any]:
        state = self.state
        if not state.vault_path:
            return _fail("Open a vault first.")
        try:
            query._refresh_pipelines()
            query._update_document_counts(state.vault_path)
        except Exception as exc:
            return _fail(str(exc))
        if not text.strip():
            return _ok(results=[], note="")
        if not state.keyword_search_enabled or not state.keyword_search_pipeline:
            return _fail(
                "Keyword search is disabled because this vault has no full-text "
                "index. Build one from the Keywords tab."
            )
        try:
            note = ""
            if apply_filter and state.filtered_keyword_search_pipeline:
                raw, filter_error = state.filtered_keyword_search_pipeline(text)
                results = query._process_semantic_results(raw)
                note = query._filter_outcome_note(len(results), filter_error)
            else:
                results = query._process_keyword_results(
                    list(state.keyword_search_pipeline(text))
                )
        except Exception as exc:
            return _fail(str(exc))
        return _ok(results=results, note=note)

    def chunk_text(self, lookup_path: str, snippet: str = "") -> dict[str, Any]:
        if not self.state.vault_path:
            return _fail("No vault is open.")
        try:
            text = query._get_chunk_text_for_path_and_snippet(
                self.state.vault_path, lookup_path, snippet
            )
        except Exception as exc:
            return _fail(f"Could not load the chunk: {exc}")
        if not text:
            return _fail("The chunk text could not be found in the vault.")
        return _ok(content=text)

    def source_file(self, lookup_path: str) -> dict[str, Any]:
        """Resolve a result to its source document on disk (``/open-file``)."""
        if not self.state.vault_path:
            return _fail("No vault is open.")
        try:
            path = query._resolve_indexed_source_file(
                self.state.vault_path, lookup_path
            )
        except Exception as exc:
            return _fail(f"Could not resolve the source document: {exc}")
        if path is None:
            return _fail("This result's source document is not in the vault index.")
        if not path.is_file():
            return _fail(f"The source document is no longer at {path}.")
        return _ok(path=str(path))

    # -- ask ----------------------------------------------------------------------

    def ask(self, message: str, *, use_keyword_search: bool = False) -> dict[str, Any]:
        """Answer a question with citations (``POST /chat``)."""
        state = self.state
        if not state.vault_path:
            return _fail("Open a vault first.")
        if not message.strip():
            return _fail("Enter a question.")
        if not state.chat_pipeline:
            return _fail("The Ask pipeline is not available; check Settings.")
        citations: list[dict[str, Any]] = []
        retrieval_note = ""
        filter_error: str | None = None
        try:
            query._refresh_pipelines()
            query._update_document_counts(state.vault_path)
            result: str | dict[str, Any]
            if use_keyword_search and state.keyword_chat_pipeline:
                result = state.keyword_chat_pipeline(message)
                response = result["response"]
                citations = query._process_semantic_results(result["background"])
                filter_error = result.get("filter_error")
                hits = result.get("keyword_hits")
                if hits:
                    retrieval_note = (
                        f"keyword search boosted retrieval ({hits} keyword "
                        f"hit{'s' if hits != 1 else ''} merged into the context)"
                    )
                else:
                    retrieval_note = (
                        "keyword search boosted retrieval (no extra keyword hits "
                        "for this question)"
                    )
            else:
                if use_keyword_search:
                    retrieval_note = (
                        "keyword search is unavailable right now — answered from "
                        "semantic retrieval only"
                    )
                result = state.chat_pipeline(message)
                if isinstance(result, dict):
                    response = result["response"]
                    citations = query._process_semantic_results(result["background"])
                    filter_error = result.get("filter_error")
                else:
                    response = result
                    citations = query._chat_citations(state, message)
            if not state.show_source_paths:
                response = query._strip_answer_source_paths(response)
        except Exception as exc:
            error = str(exc)
            lowered = error.lower()
            if "ollama" in lowered and ("connect" in lowered or "refused" in lowered):
                error += " Tip: set the Ollama server URL under Settings → Connections."
            elif ("openai" in lowered or "anthropic" in lowered) and (
                "api key" in lowered or "api_key" in lowered or "credential" in lowered
            ):
                error += " Tip: enter the API key under Settings → Connections."
            return _fail(error)
        if not state.show_source_paths:
            citations = [{k: v for k, v in c.items() if k != "path"} for c in citations]
        answered_by = query._answered_by(state)
        if retrieval_note:
            answered_by = f"{answered_by} · {retrieval_note}"
        if state.result_filter_active:
            answered_by += (
                f" · the vault's retrieval filter failed and was skipped ({filter_error})"
                if filter_error
                else " · the vault's retrieval filter was applied"
            )
        elif state.result_filter_error:
            answered_by += (
                " · the vault's retrieval filter does not compile and was not applied"
            )
        return _ok(answer=response, citations=citations, answered_by=answered_by)

    # -- settings -----------------------------------------------------------------

    def settings_view(self) -> dict[str, Any]:
        state = self.state
        return {
            "models": query._effective_models(state),
            "overrides": {
                "embedding_source": state.embedding_source or "",
                "embedding_model": state.embedding_model or "",
                "chat_source": state.chat_source or "",
                "chat_model": state.chat_model or "",
                "chunk_size": state.chunk_size,
                "shingle_size": state.shingle_size,
                "shingle_overlap": state.shingle_overlap,
                "rag_result_limit": state.rag_result_limit,
            },
            "embedding_sources": list(getEmbeddingSources()),
            "chat_sources": list(getPromptSources()),
            "credentials": credentials.describe(),
            "credentials_store": str(credentials.store_path()),
            "settings_file": str(user_settings.settings_file_path()),
        }

    def save_settings(
        self,
        *,
        embedding_source: str = "",
        embedding_model: str = "",
        chat_source: str = "",
        chat_model: str = "",
        chunk_size: str = "",
        shingle_size: str = "",
        shingle_overlap: str = "",
        rag_result_limit: str = "",
    ) -> dict[str, Any]:
        state = self.state
        previous = query._effective_models(state)
        minimums = user_settings.INTEGER_SETTING_MINIMUMS
        try:
            chunk = query._parse_int_setting(
                chunk_size, "Chunk size", minimums["chunk_size"]
            )
            shingle = query._parse_int_setting(
                shingle_size, "Shingle size", minimums["shingle_size"]
            )
            overlap = query._parse_int_setting(
                shingle_overlap, "Shingle overlap", minimums["shingle_overlap"]
            )
            limit = query._parse_int_setting(
                rag_result_limit, "Ask result count", minimums["rag_result_limit"]
            )
        except ValueError as exc:
            return _fail(str(exc))
        if shingle is not None and overlap is not None and overlap >= shingle:
            return _fail("Shingle overlap must be smaller than shingle size.")
        user_settings.save_model_overrides(
            embedding_source=embedding_source,
            embedding_model=embedding_model,
            chat_source=chat_source,
            chat_model=chat_model,
            chunk_size=chunk,
            shingle_size=shingle,
            shingle_overlap=overlap,
            rag_result_limit=limit,
        )
        query.load_saved_model_overrides()
        if state.vault_path:
            try:
                query._refresh_pipelines(force=True)
            except Exception as exc:
                return _ok(
                    "Settings saved, but the pipelines could not be rebuilt with "
                    f"them: {exc}"
                )
        models = query._effective_models(state)
        message = "Settings saved."
        if (previous["embedding_source"], previous["embedding_model"]) != (
            models["embedding_source"],
            models["embedding_model"],
        ):
            message += (
                " The embedding model changed: existing vaults were indexed with "
                "the previous model, so re-index their documents (with Overwrite) "
                "before searching them."
            )
        return _ok(message)

    def save_credentials(self, changes: dict[str, str | None]) -> dict[str, Any]:
        """Persist connection settings; secrets set to "" are cleared."""
        try:
            credentials.set_values(changes)
        except Exception as exc:
            return _fail(f"Could not save connection settings: {exc}")
        if self.state.vault_path:
            try:
                query._refresh_pipelines(force=True)
            except Exception as exc:
                return _ok(f"Connection settings saved; pipelines not rebuilt: {exc}")
        return _ok("Connection settings saved for this app.")

    def config_status(
        self, *, probe: bool = True, download: bool = False
    ) -> dict[str, Any]:
        try:
            return query._collect_config_status(
                self.state, probe=probe, allow_download=download
            )
        except Exception as exc:
            return {
                "overall": "error",
                "checks": [
                    {
                        "name": "Configuration status",
                        "status": "error",
                        "value": "",
                        "summary": f"The status check itself failed: {exc}",
                    }
                ],
            }

    # -- retrieval filter ---------------------------------------------------------

    def filter_view(self) -> dict[str, Any]:
        return query._retrieval_filter_context(self.state)

    def save_filter(
        self, *, action: str, script: str, enabled: bool, strict: bool
    ) -> dict[str, Any]:
        state = self.state
        if not state.vault_path:
            return _fail("Open a vault before configuring its retrieval filter.")
        if action == "remove":
            retrieval_filter.remove_script(state.vault_path)
            user_settings.clear_retrieval_filter_flags(state.vault_path)
            query._refresh_pipelines(force=True)
            return _ok("Retrieval filter removed.")
        if not script.strip():
            return _fail("Enter a script, or use Remove to delete the saved one.")
        error = retrieval_filter.validate_script(script)
        if action == "validate":
            return _fail(error) if error else _ok("The script compiles.")
        if error:
            return _fail(error)
        retrieval_filter.save_script(state.vault_path, script)
        user_settings.set_retrieval_filter_flags(
            state.vault_path, enabled=enabled, strict=strict
        )
        query._refresh_pipelines(force=True)
        return _ok(
            f"Retrieval filter {'enabled' if enabled else 'saved but not enabled'}."
        )

    # -- misc ---------------------------------------------------------------------

    def vault_embedding_record(self) -> dict[str, Any] | None:
        if not self.state.vault_path:
            return None
        return vault_metadata.load_embedding_config(self.state.vault_path)
