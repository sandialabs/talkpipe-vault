import logging
from typing import Literal, Annotated
from pydantic import BaseModel
from docling.document_converter import DocumentConverter
from talkpipe import AbstractFieldSegment, register_segment

logger = logging.getLogger(__name__)


class OutputFormat(BaseModel):
    format: Literal["markdown", "plain_text"]

@register_segment("doclingToText")
class DoclingFileToText(AbstractFieldSegment):
    """
    Segment that extracts text from a file path using Docling.

    This segment takes a file path as input and uses the Docling library
    to extract and return the text content from the specified file.
    If conversion fails, a warning is logged and the item is skipped.
    """

    def __init__(self,
                 field: Annotated[str, "The field to extract.  If none, use full item."] = None,
                 set_as: Annotated[str, "The field to set/append the result as."] = None):
        super().__init__(field=field, set_as=set_as, multi_emit=False)
        self.converter = DocumentConverter()

    def process_value(self, input_data: Annotated[str, "Input file path"]) -> str | None:
        try:
            # Handle plain text files directly
            if input_data.lower().endswith('.txt'):
                with open(input_data, 'r', encoding='utf-8') as f:
                    return f.read()

            result = self.converter.convert(input_data)
            return result.document.export_to_markdown()
        except Exception as e:
            logger.warning(f"Failed to convert '{input_data}': {e}")
            return None
