from typing import Literal, Annotated
from pydantic import BaseModel
from docling.document_converter import DocumentConverter
from talkpipe import AbstractFieldSegment, register_segment

class OutputFormat(BaseModel):
    format: Literal["markdown", "plain_text"]

@register_segment("pathToText")
class DoclingPathToText(AbstractFieldSegment):
    """
    Segment that extracts text from a file path using Docling.

    This segment takes a file path as input and uses the Docling library
    to extract and return the text content from the specified file.
    """

    def __init__(self,
                 field: Annotated[str, "The field to extract.  If none, use full item."] = None, 
                 set_as: Annotated[str, "The field to set/append the result as."] = None):
        super().__init__(field=field, set_as=set_as, multi_emit=False)
        self.converter = DocumentConverter()

    def process_value(self, input_data: str) -> str:
        result = self.converter.convert(input_data)
        return result.document.export_to_markdown()
