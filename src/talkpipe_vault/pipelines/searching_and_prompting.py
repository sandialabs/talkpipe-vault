from typing import Annotated
from talkpipe.pipe import field_segment
from talkpipe.chatterlang import register_segment
from talkpipe.pipe.basic import ToDict, AbstractFieldSegment, fillTemplate, EvalExpression
from talkpipe.pipe.io import Print
from talkpipe.pipelines.basic_rag import RAGToText
from talkpipe.pipelines.vector_databases import SearchVectorDatabaseSegment
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
        path: Annotated[str, "Path to the vault"],
        field: Annotated[str, "The field to extract.  If none, use full item."] = None, 
        set_as: Annotated[str, "The field to set/append the result as."] = None, 
        multi_emit: Annotated[bool, "Whether this class potentially emits multiple results per item."
                                    "Should be set by the subclass constructor call or the field_segment decorator, not by the user."] = False):
        super().__init__(field=field, set_as=set_as, multi_emit=multi_emit)
        self.path = path
        self.pipeline = (ToDict(field_list="_:query") |  \
            fillTemplate(template=RETRIEVAL_TEMPLATE, set_as="templated_query") | \
            SearchVectorDatabaseSegment(
                path=path,
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
        path: Annotated[str, "Path to the vault"],
        field: Annotated[str, "The field to extract.  If none, use full item."] = None, 
        set_as: Annotated[str, "The field to set/append the result as."] = None, 
        multi_emit: Annotated[bool, "Whether this class potentially emits multiple results per item."
                                    "Should be set by the subclass constructor call or the field_segment decorator, not by the user."] = False):
        super().__init__(field=field, set_as=set_as, multi_emit=multi_emit)
        self.path = path
        self.pipeline = (ToDict(field_list="_:query") | \
            fillTemplate(template=RETRIEVAL_TEMPLATE, set_as="templated_query") | \
            RAGToText(
                path=path,
                content_field="query",
                embedding_prompt="templated_query",
                table_name="shingled_chunks",
                set_as="chat_response"
            ) | \
            EvalExpression(field="chat_response", expression="item")).as_function(single_in=True, single_out=True)

    def process_value(self, value):
        return self.pipeline(value)


