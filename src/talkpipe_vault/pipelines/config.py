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
   EMBEDDING_MODEL="embeddinggemma"
   EMBEDDING_SOURCE="ollama"
   CHAT_MODEL="mistral-small"
   CHAT_SOURCE="ollama"
   DOCUMENT_TEMPLATE="title: {title} | text: {content}"
   SHINGLE_TEMPLATE="title: {title} | text: {shingle}"
   RETRIEVAL_TEMPLATE="task: search result | query: {query}"
"""

import os

from talkpipe.util.config import get_config
from talkpipe.util.data_manipulation import dict_to_text

# Default values (used if not specified in TalkPipe config)
EMBEDDING_MODEL = "embeddinggemma"
EMBEDDING_SOURCE = "ollama"
# CHAT_MODEL="gpt-oss:latest"
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
    """
    Get embedding model from TalkPipe config or default.

    Checks for configuration in this order:
    1. TalkPipe config: 'embedding_model' (in [vault] section or top-level)
    2. TalkPipe config: 'EMBEDDING_MODEL' (uppercase variant)
    3. TalkPipe config: 'default_embedding_model_name' (standard TalkPipe key)
    4. Default: EMBEDDING_MODEL constant

    Returns:
        str: Embedding model name
    """
    return _get_talkpipe_config_value(
        "embedding_model",
        EMBEDDING_MODEL,
        alternative_keys=["default_embedding_model_name", "EMBEDDING_MODEL"],
    )


def get_embedding_source() -> str:
    """
    Get embedding source from TalkPipe config or default.

    Checks for configuration in this order:
    1. TalkPipe config: 'embedding_source' (in [vault] section or top-level)
    2. TalkPipe config: 'EMBEDDING_SOURCE' (uppercase variant)
    3. TalkPipe config: 'default_embedding_model_source' (standard TalkPipe key)
    4. Default: EMBEDDING_SOURCE constant

    Returns:
        str: Embedding source/provider (e.g., 'ollama', 'openai')
    """
    return _get_talkpipe_config_value(
        "embedding_source",
        EMBEDDING_SOURCE,
        alternative_keys=["default_embedding_model_source", "EMBEDDING_SOURCE"],
    )


VECTOR_VAULT_SUBDIR = "vector_vault"
FULLTEXT_VAULT_SUBDIR = "fulltext_vault"


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
    return (
        os.path.join(vault_path, VECTOR_VAULT_SUBDIR),
        os.path.join(vault_path, FULLTEXT_VAULT_SUBDIR),
    )


def get_chat_model() -> str:
    """
    Get chat model from TalkPipe config or default.

    Checks for configuration in this order:
    1. TalkPipe config: 'chat_model' (in [vault] section or top-level)
    2. TalkPipe config: 'CHAT_MODEL' (uppercase variant)
    3. TalkPipe config: 'default_model_name' (standard TalkPipe key)
    4. Default: CHAT_MODEL constant

    Returns:
        str: Chat/completion model name
    """
    return _get_talkpipe_config_value(
        "chat_model", CHAT_MODEL, alternative_keys=["default_model_name", "CHAT_MODEL"]
    )


def get_chat_source() -> str:
    """
    Get chat source from TalkPipe config or default.

    Checks for configuration in this order:
    1. TalkPipe config: 'chat_source' (in [vault] section or top-level)
    2. TalkPipe config: 'CHAT_SOURCE' (uppercase variant)
    3. TalkPipe config: 'default_model_source' (standard TalkPipe key)
    4. Default: CHAT_SOURCE constant

    Returns:
        str: Chat source/provider (e.g., 'ollama', 'openai')
    """
    return _get_talkpipe_config_value(
        "chat_source",
        CHAT_SOURCE,
        alternative_keys=["default_model_source", "CHAT_SOURCE"],
    )


RAG_PREFIX_PROMPTS = dict_to_text(
    {
        "developer": """You are a helpful assistant that answers questions based on provided background information.
Ground your responses in the background context given. If the background does not contain sufficient information to answer the question, acknowledge this limitation rather than speculating or making up information.
Be concise and accurate in your responses.  Make it clear which answers are from general knowledge and which are from the provided content. List the files used to inform your answer."""
    }
)
RAG_PROMPT_DIRECTIVE = "Remember to list the files you used to inform your answer."


# Default template values (used if not specified in TalkPipe config)
DOCUMENT_TEMPLATE = """title: {title} | text: {content}"""
SHINGLE_TEMPLATE = """title: {title} | text: {shingle}"""
RETRIEVAL_TEMPLATE = """task: search result | query: {query}"""


def get_document_template() -> str:
    """
    Get document template from TalkPipe config or default.

    Checks for configuration in this order:
    1. TalkPipe config: 'document_template' (in [vault] section or top-level)
    2. TalkPipe config: 'DOCUMENT_TEMPLATE' (uppercase variant)
    3. Default: DOCUMENT_TEMPLATE constant

    The template is used to format full documents before embedding.
    Available placeholders: {title}, {content}

    Returns:
        str: Document template string
    """
    return _get_talkpipe_config_value(
        "document_template", DOCUMENT_TEMPLATE, alternative_keys=["DOCUMENT_TEMPLATE"]
    )


def get_shingle_template() -> str:
    """
    Get shingle template from TalkPipe config or default.

    Checks for configuration in this order:
    1. TalkPipe config: 'shingle_template' (in [vault] section or top-level)
    2. TalkPipe config: 'SHINGLE_TEMPLATE' (uppercase variant)
    3. Default: SHINGLE_TEMPLATE constant

    The template is used to format shingled chunks before embedding.
    Available placeholders: {title}, {shingle}

    Returns:
        str: Shingle template string
    """
    return _get_talkpipe_config_value(
        "shingle_template", SHINGLE_TEMPLATE, alternative_keys=["SHINGLE_TEMPLATE"]
    )


def get_retrieval_template() -> str:
    """
    Get retrieval template from TalkPipe config or default.

    Checks for configuration in this order:
    1. TalkPipe config: 'retrieval_template' (in [vault] section or top-level)
    2. TalkPipe config: 'RETRIEVAL_TEMPLATE' (uppercase variant)
    3. Default: RETRIEVAL_TEMPLATE constant

    The template is used to format search queries before embedding.
    Available placeholders: {query}

    Returns:
        str: Retrieval template string
    """
    return _get_talkpipe_config_value(
        "retrieval_template",
        RETRIEVAL_TEMPLATE,
        alternative_keys=["RETRIEVAL_TEMPLATE"],
    )
