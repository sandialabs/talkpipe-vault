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
    Segment that extracts text from files using Docling for documents or direct reading for source code.

    Expects input items containing a file path string (either as the full item or in a specified field).
    Source code and plain text files (.py, .js, .txt, etc.) are read directly. Document formats
    (PDF, DOCX, etc.) are converted via Docling and exported as markdown.

    Emits items with extracted text content (as full item or in a specified field).
    Returns None for items that fail conversion, which are then skipped.
    """

    def __init__(self,
                 field: Annotated[str, "The field to extract.  If none, use full item."] = None,
                 set_as: Annotated[str, "The field to set/append the result as."] = None):
        super().__init__(field=field, set_as=set_as, multi_emit=False)
        self.converter = DocumentConverter()

    def process_value(self, input_data: Annotated[str, "Input file path"]) -> str | None:
        # Source code and plain text extensions to read directly
        text_extensions = {
            '.txt', '.rst',  # Plain text and documentation
            '.py', '.pyw', '.pyx',  # Python
            '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs',  # JavaScript/TypeScript
            '.java', '.kt', '.kts', '.scala',  # JVM languages
            '.c', '.h', '.cpp', '.hpp', '.cc', '.cxx', '.c++', '.h++',  # C/C++
            '.cs', '.vb',  # .NET languages
            '.go', '.rs', '.swift',  # Go, Rust, Swift
            '.rb', '.php', '.pl', '.pm',  # Ruby, PHP, Perl
            '.sh', '.bash', '.zsh', '.fish',  # Shell scripts
            '.r', '.R',  # R
            '.sql', '.psql',  # SQL
            '.css', '.scss', '.sass', '.less',  # Stylesheets
            '.xml', '.svg',  # Markup
            '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf',  # Config files
            '.vim', '.lua', '.tcl',  # Other scripting
            '.m', '.mm',  # Objective-C
            '.f', '.f90', '.f95',  # Fortran
            '.asm', '.s',  # Assembly
            '.diff', '.patch',  # Patches
            '.log',  # Log files
        }

        try:
            # Handle text and source code files directly
            rp = input_data.lower().rpartition('.')
            file_ext = ''.join(rp[1:])  # Get file extension with dot

            if file_ext in text_extensions:
                with open(input_data, 'r', encoding='utf-8') as f:
                    return f.read()
            else:
                # Use Docling for document formats (PDF, DOCX, etc.)
                result = self.converter.convert(input_data)
                return result.document.export_to_markdown()
        except Exception as e:
            logger.warning(f"Failed to convert '{input_data}': {e}")
            return None
