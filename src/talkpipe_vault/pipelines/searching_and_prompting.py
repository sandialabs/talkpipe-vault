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
    Segment that searches a vector database built from a vault.
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
    Segment that handles chat interactions with a vault.
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


