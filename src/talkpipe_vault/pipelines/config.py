EMBEDDING_MODEL="embeddinggemma"
EMBEDDING_SOURCE="ollama"

DOCUMENT_TEMPLATE="""title: {filename} | text: {full_content}"""
SHINGLE_TEMPLATE="""title: {filename} | text: {shingle}"""
RETRIEVAL_TEMPLATE="""task: search result | query: {query}"""