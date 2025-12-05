import os
from typing import Annotated
from talkpipe.pipe import field_segment
from talkpipe.chatterlang import register_segment
from talkpipe.pipe.basic import ToDict, AbstractFieldSegment, fillTemplate, EvalExpression
from talkpipe.pipe.io import Print
from talkpipe.pipelines.basic_rag import RAGToText
from talkpipe.pipelines.vector_databases import SearchVectorDatabaseSegment
from talkpipe.search.whoosh import searchWhoosh
from .config import RETRIEVAL_TEMPLATE, EMBEDDING_MODEL, EMBEDDING_SOURCE

@register_segment("vaultSearch")
class VaultSearch(AbstractFieldSegment):
    """
    Segment that performs semantic search on a vault's vector database.

    Expects input items containing a search query string (either as the full item
    or in a specified field). The query is templated and used to search the
    'shingled_chunks' table in LanceDB.

    Emits search results from the vector database containing matching document chunks.
    """
    def __init__(
        self,
        vault_path: Annotated[str, "Base path for vault storage. Vector DB located at vault_path/vector_vault"],
        field: Annotated[str, "The field to extract.  If none, use full item."] = None,
        set_as: Annotated[str, "The field to set/append the result as."] = None,
        multi_emit: Annotated[bool, "Whether this class potentially emits multiple results per item."
                                    "Should be set by the subclass constructor call or the field_segment decorator, not by the user."] = False):
        super().__init__(field=field, set_as=set_as, multi_emit=multi_emit)
        self.vault_path = vault_path
        vectordb_path = os.path.join(vault_path, "vector_vault")
        self.pipeline = (ToDict(field_list="_:query") |  \
            fillTemplate(template=RETRIEVAL_TEMPLATE, set_as="templated_query") | \
            SearchVectorDatabaseSegment(
                path=vectordb_path,
                table_name="shingled_chunks",
                query_field="templated_query",
                embedding_model=EMBEDDING_MODEL,
                embedding_source=EMBEDDING_SOURCE,
            )).as_function(single_in=True, single_out=True)

    def process_value(self, value):
        return self.pipeline(value)

@register_segment("vaultChat")
class VaultChat(AbstractFieldSegment):
    """
    Segment that provides RAG-based conversational AI using vault contents.

    Expects input items containing a user query string (either as the full item
    or in a specified field). The query is used to retrieve relevant context from
    the vault's 'shingled_chunks' table, which is then used to generate a response.

    Emits AI-generated response strings based on retrieved vault context.
    """
    def __init__(
        self,
        vault_path: Annotated[str, "Base path for vault storage. Vector DB located at vault_path/vector_vault"],
        field: Annotated[str, "The field to extract.  If none, use full item."] = None,
        set_as: Annotated[str, "The field to set/append the result as."] = None,
        multi_emit: Annotated[bool, "Whether this class potentially emits multiple results per item."
                                    "Should be set by the subclass constructor call or the field_segment decorator, not by the user."] = False):
        super().__init__(field=field, set_as=set_as, multi_emit=multi_emit)
        self.vault_path = vault_path
        vectordb_path = os.path.join(vault_path, "vector_vault")
        self.pipeline = (ToDict(field_list="_:query") | \
            fillTemplate(template=RETRIEVAL_TEMPLATE, set_as="templated_query") | \
            RAGToText(
                path=vectordb_path,
                content_field="query",
                embedding_prompt="templated_query",
                table_name="shingled_chunks",
                set_as="chat_response",
                prompt_directive="""You are a research assistant. Answer the question using ONLY the provided context.
Each context item has a Filename and Path field - these identify the source documents.

After your answer, you MUST include a "Sources:" section. This is REQUIRED - never skip it.
List each unique document you used as: - Filename (Path)

Example format for the end of your response:

Sources:
- paper.pdf (/docs/paper.pdf)
- notes.txt (/docs/notes.txt)

Remember: ALWAYS end with Sources section.""",
            ) | \
            EvalExpression(field="chat_response", expression="item")).as_function(single_in=True, single_out=True)

    def process_value(self, value):
        return self.pipeline(value)


@register_segment("vaultTextSearch")
class VaultTextSearch(AbstractFieldSegment):
    """
    Segment that performs full-text search on a vault's Whoosh index.

    Expects input items containing a search query string (either as the full item
    or in a specified field). The query uses Whoosh query syntax for keyword-based
    searching of the 'content' field in the fulltext_vault index.

    Emits search results as dicts containing:
        - "doc_id": str - Document identifier (file path)
        - "score": float - Relevance score
        - "document": dict - Contains "content" field with matched text
    """
    def __init__(
        self,
        vault_path: Annotated[str, "Base path for vault storage. Whoosh index located at vault_path/fulltext_vault"],
        limit: Annotated[int, "Maximum number of results to return"] = 10,
        field: Annotated[str, "The field to extract. If none, use full item."] = None,
        set_as: Annotated[str, "The field to set/append the result as."] = None,
        multi_emit: Annotated[bool, "Whether this class potentially emits multiple results per item."
                                    "Should be set by the subclass constructor call or the field_segment decorator, not by the user."] = True):
        super().__init__(field=field, set_as=set_as, multi_emit=multi_emit)
        self.vault_path = vault_path
        whoosh_index_path = os.path.join(vault_path, "fulltext_vault")
        self.pipeline = searchWhoosh(
            index_path=whoosh_index_path,
            limit=limit,
            all_results_at_once=False
        ).as_function(single_in=True, single_out=False)

    def process_value(self, value):
        return list(self.pipeline(value))


