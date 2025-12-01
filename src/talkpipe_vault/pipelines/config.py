EMBEDDING_MODEL="embeddinggemma"
EMBEDDING_SOURCE="ollama"

DOCUMENT_TEMPLATE="""title: none | text: {full_content}"""
SHINGLE_TEMPLATE="""title: none | text: {shingle}"""
RETRIEVAL_TEMPLATE="""task: search result | query: {query}"""