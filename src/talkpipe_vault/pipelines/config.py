"""
Configuration module for TalkPipe Vault.

This module provides default model configurations and helper functions to retrieve
configuration values from TalkPipe's configuration system.

Configuration Precedence (highest to lowest):
1. Explicit parameters passed to segments/sources
2. TalkPipe configuration (from ~/.talkpipe.toml or TALKPIPE_* environment variables)
3. Default values defined in this module

Supported Configuration Keys:
- embedding_model: Model name for generating embeddings
  Alternative keys: EMBEDDING_MODEL, default_embedding_model_name
- embedding_source: Provider for embedding model (e.g., 'ollama', 'openai')
  Alternative keys: EMBEDDING_SOURCE, default_embedding_model_source
- chat_model: Model name for chat/completion
  Alternative keys: CHAT_MODEL, default_model_name
- chat_source: Provider for chat model (e.g., 'ollama', 'openai')
  Alternative keys: CHAT_SOURCE, default_model_source
- document_template: Template for formatting full documents before embedding
  Alternative keys: DOCUMENT_TEMPLATE
  Placeholders: {title}, {content}
- shingle_template: Template for formatting shingled chunks before embedding
  Alternative keys: SHINGLE_TEMPLATE
  Placeholders: {title}, {shingle}
- retrieval_template: Template for formatting search queries before embedding
  Alternative keys: RETRIEVAL_TEMPLATE
  Placeholders: {query}

Configuration can be set via:
1. TalkPipe config file (~/.talkpipe.toml):
   [vault]
   embedding_model = "text-embedding-3-large"
   embedding_source = "openai"

2. Environment variables:
   export TALKPIPE_EMBEDDING_MODEL="text-embedding-3-large"
   export TALKPIPE_EMBEDDING_SOURCE="openai"

3. Default values (if not configured):
   EMBEDDING_MODEL="minishlab/potion-retrieval-32M"
   EMBEDDING_SOURCE="model2vec"
   CHAT_MODEL="mistral-small"
   CHAT_SOURCE="ollama"
   DOCUMENT_TEMPLATE="title: {title} | text: {content}"
   SHINGLE_TEMPLATE="title: {title} | text: {shingle}"
   RETRIEVAL_TEMPLATE="task: search result | query: {query}"
"""

import os

from talkpipe.util.config import get_config

# Default values (used if not specified in TalkPipe config).
# model2vec runs in-process (no server or API key); the model is downloaded
# from Hugging Face on first use and cached locally.
EMBEDDING_MODEL = "minishlab/potion-retrieval-32M"
EMBEDDING_SOURCE = "model2vec"
CHAT_MODEL = "mistral-small"
CHAT_SOURCE = "ollama"


def _get_talkpipe_config_value(
    key: str, default: str, alternative_keys: list[str] | None = None
) -> str:
    """
    Get a configuration value from TalkPipe config, falling back to default.

    Checks TalkPipe configuration (from ~/.talkpipe.toml or TALKPIPE_* env vars)
    for the given key. If not found, returns the default value.

    Args:
        key: Primary configuration key to look for (e.g., 'embedding_model')
        default: Default value to use if key is not found in TalkPipe config
        alternative_keys: Alternative key names to check (e.g., ['default_embedding_model_name'])

    Returns:
        Configuration value from TalkPipe config if present, otherwise default
    """
    config = get_config()
    # Check multiple possible locations and key names
    keys_to_check = [key, key.upper()]  # Check both lowercase and uppercase
    if alternative_keys:
        keys_to_check.extend(alternative_keys)

    # Check in vault section first, then top level
    for check_key in keys_to_check:
        value = config.get("vault", {}).get(check_key) or config.get(check_key)
        if value is not None:
            return value

    return default


def get_embedding_model() -> str:
    """Embedding model from TalkPipe config or default (see module docstring
    for key precedence)."""
    return _get_talkpipe_config_value(
        "embedding_model",
        EMBEDDING_MODEL,
        alternative_keys=["default_embedding_model_name", "EMBEDDING_MODEL"],
    )


def get_embedding_source() -> str:
    """Embedding source/provider from TalkPipe config or default (see module
    docstring for key precedence)."""
    return _get_talkpipe_config_value(
        "embedding_source",
        EMBEDDING_SOURCE,
        alternative_keys=["default_embedding_model_source", "EMBEDDING_SOURCE"],
    )


VECTOR_VAULT_SUBDIR = "vector_vault"
FULLTEXT_VAULT_SUBDIR = "fulltext_vault"
DEFAULT_VECTOR_TABLE_NAME = "docs"


def resolve_embedding_config(
    embedding_model: str | None,
    embedding_source: str | None,
) -> tuple[str, str]:
    """
    Resolve embedding model and source from explicit params or config defaults.

    Returns:
        tuple[str, str]: (embedding_model, embedding_source)
    """
    return (
        embedding_model if embedding_model is not None else get_embedding_model(),
        embedding_source if embedding_source is not None else get_embedding_source(),
    )


def get_vault_paths(vault_path: str) -> tuple[str, str]:
    """
    Return vault storage paths for vector DB and Whoosh index.

    Returns:
        tuple[str, str]: (vectordb_path, whoosh_index_path)
    """
    return (get_vector_db_path(vault_path), get_whoosh_index_path(vault_path))


def get_vector_db_path(vault_path: str) -> str:
    """
    Return the LanceDB path expected by makevectordatabase/serverag.

    ``~`` is expanded so a path like ``~/my-vault`` refers to the home
    directory, matching what LanceDB itself does with the same path.
    """
    return os.path.expanduser(vault_path)


def get_whoosh_index_path(vault_path: str) -> str:
    """
    Return the Whoosh index path under the configured vault path.

    ``~`` is expanded so the index lands inside the real vault directory
    rather than a literal ``./~`` folder (LanceDB expands the same path, so
    without this the vault's tables and full-text index would split across
    two locations).
    """
    return os.path.join(os.path.expanduser(vault_path), FULLTEXT_VAULT_SUBDIR)


def ensure_supported_vault_layout(vault_path: str) -> None:
    """
    Enforce direct LanceDB layout and reject legacy nested vector_vault layout.

    Expects LanceDB files/tables directly under ``vault_path`` and Whoosh index under
    ``vault_path/fulltext_vault``.
    """
    legacy_vector_path = os.path.join(vault_path, VECTOR_VAULT_SUBDIR)
    if os.path.isdir(legacy_vector_path):
        raise ValueError(
            "Unsupported legacy vault layout detected at "
            f"{legacy_vector_path}. Expected LanceDB tables directly under "
            f"{vault_path} (same as makevectordatabase output). "
            "Migrate by moving LanceDB contents from vector_vault/ into the "
            "vault root path, then retry."
        )


def get_chat_model() -> str:
    """Chat/completion model from TalkPipe config or default (see module
    docstring for key precedence)."""
    return _get_talkpipe_config_value(
        "chat_model", CHAT_MODEL, alternative_keys=["default_model_name", "CHAT_MODEL"]
    )


def get_chat_source() -> str:
    """Chat source/provider from TalkPipe config or default (see module
    docstring for key precedence)."""
    return _get_talkpipe_config_value(
        "chat_source",
        CHAT_SOURCE,
        alternative_keys=["default_model_source", "CHAT_SOURCE"],
    )


# Passed to RAGToText as ``system_prompt`` (a plain string), not ``role_map``.
# role_map is parsed as comma-separated ``role:message`` pairs, so any comma in
# this text would be split into bogus roles that strict providers (e.g. OpenAI)
# reject with a 400; a system_prompt is sent verbatim as a single system message.
RAG_SYSTEM_PROMPT = """You are a helpful assistant that answers questions based on provided background information.
Ground your responses in the background context given. If the background does not contain sufficient information to answer the question, acknowledge this limitation rather than speculating or making up information.
Be concise and accurate in your responses.  Make it clear which answers are from general knowledge and which are from the provided content. List the files used to inform your answer. Refer to files by their file name or title only; never include directory paths."""
RAG_PROMPT_DIRECTIVE = (
    "Remember to list the files you used to inform your answer, "
    "by file name or title only (no directory paths)."
)


# Default template values (used if not specified in TalkPipe config)
DOCUMENT_TEMPLATE = """title: {title} | text: {content}"""
SHINGLE_TEMPLATE = """title: {title} | text: {shingle}"""
RETRIEVAL_TEMPLATE = """task: search result | query: {query}"""


def get_document_template() -> str:
    """Template for formatting full documents before embedding (placeholders
    {title}, {content}); see module docstring for key precedence."""
    return _get_talkpipe_config_value(
        "document_template", DOCUMENT_TEMPLATE, alternative_keys=["DOCUMENT_TEMPLATE"]
    )


def get_shingle_template() -> str:
    """Template for formatting shingled chunks before embedding (placeholders
    {title}, {shingle}); see module docstring for key precedence."""
    return _get_talkpipe_config_value(
        "shingle_template", SHINGLE_TEMPLATE, alternative_keys=["SHINGLE_TEMPLATE"]
    )


def get_retrieval_template() -> str:
    """Template for formatting search queries before embedding (placeholder
    {query}); see module docstring for key precedence."""
    return _get_talkpipe_config_value(
        "retrieval_template",
        RETRIEVAL_TEMPLATE,
        alternative_keys=["RETRIEVAL_TEMPLATE"],
    )
