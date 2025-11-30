from typing import Annotated
from talkpipe.pipe import field_segment
from talkpipe.chatterlang import register_segment
from talkpipe.pipe.basic import ToDict, AbstractFieldSegment
from talkpipe.pipe.io import Print
from talkpipe.pipelines.basic_rag import RAGToText
from talkpipe.pipelines.vector_databases import SearchVectorDatabaseSegment

@register_segment("vaultSearch")
class VaultSearch(AbstractFieldSegment):
    def __init__(
        self,
        path: Annotated[str, "Path to the vault"],
        field: Annotated[str, "The field to extract.  If none, use full item."] = None, 
        set_as: Annotated[str, "The field to set/append the result as."] = None, 
        multi_emit: Annotated[bool, "Whether this class potentially emits multiple results per item."
                                    "Should be set by the subclass constructor call or the field_segment decorator, not by the user."] = False):
        super().__init__(field=field, set_as=set_as, multi_emit=multi_emit)
        self.path = path
        self.pipeline = (ToDict(field_list="_:original") | \
            SearchVectorDatabaseSegment(
                path=path,
                table_name="shingled_chunks"
            )).as_function(single_in=True, single_out=True)
        
    def process_value(self, value):
        return self.pipeline(value)

@register_segment("vaultChat")
class VaultChat(AbstractFieldSegment):
    def __init__(
        self,
        path: Annotated[str, "Path to the vault"],
        field: Annotated[str, "The field to extract.  If none, use full item."] = None, 
        set_as: Annotated[str, "The field to set/append the result as."] = None, 
        multi_emit: Annotated[bool, "Whether this class potentially emits multiple results per item."
                                    "Should be set by the subclass constructor call or the field_segment decorator, not by the user."] = False):
        super().__init__(field=field, set_as=set_as, multi_emit=multi_emit)
        self.path = path
        self.pipeline = (ToDict(field_list="_:original") | \
            RAGToText(
                path=path,
                content_field="original",
                table_name="shingled_chunks"
            )).as_function(single_in=True, single_out=True)

    def process_value(self, value):
        return self.pipeline(value)


