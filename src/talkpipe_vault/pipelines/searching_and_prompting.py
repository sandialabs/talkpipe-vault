"""The vault's search and RAG components, under their original import path.

The implementation is split by function; import from the module that holds
what you need, or from here:

- ``search_results`` — the result shape, ``normalize_search_result``,
  ``reciprocal_rank_fusion``, ``mergeSearchResults``, ``filterSearchResults``
- ``keyword_search`` — ``searchLance``, ``extractSearchKeywords``,
  ``keyword_query_from_llm_output``, ``words_query``
- ``vault_paths`` — ``resolve_vault_paths``, ``check_searchable``,
  ``needs_fan_out`` and the mixin the search segments share
- ``vault_search`` — ``vaultSearch``, ``vaultTextSearch``
- ``vault_chat`` — ``vaultChat``
"""

from .keyword_search import (
    ExtractSearchKeywords,
    keyword_query_from_llm_output,
    searchLance,
    words_query,
)
from .search_results import (
    RRF_K,
    SEARCH_RESULT_RENAMES,
    MergeSearchResults,
    SearchResultFilter,
    normalize_document_cell,
    normalize_search_result,
    reciprocal_rank_fusion,
    search_result_to_dict,
)
from .vault_chat import VaultChat
from .vault_paths import check_searchable, needs_fan_out, resolve_vault_paths
from .vault_search import VaultSearch, VaultTextSearch

__all__ = [
    "RRF_K",
    "SEARCH_RESULT_RENAMES",
    "ExtractSearchKeywords",
    "MergeSearchResults",
    "SearchResultFilter",
    "VaultChat",
    "VaultSearch",
    "VaultTextSearch",
    "check_searchable",
    "keyword_query_from_llm_output",
    "needs_fan_out",
    "normalize_document_cell",
    "normalize_search_result",
    "reciprocal_rank_fusion",
    "resolve_vault_paths",
    "searchLance",
    "search_result_to_dict",
    "words_query",
]
