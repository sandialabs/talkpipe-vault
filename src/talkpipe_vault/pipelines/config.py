from talkpipe.util.data_manipulation import dict_to_text

EMBEDDING_MODEL="embeddinggemma"
EMBEDDING_SOURCE="ollama"
CHAT_MODEL="gpt-oss:latest"
CHAT_SOURCE="ollama"

RAG_PREFIX_PROMPTS = dict_to_text({
    "developer":"""You are a helpful assistant that answers questions based on provided background information.
Ground your responses in the background context given. If the background does not contain sufficient information to answer the question, acknowledge this limitation rather than speculating or making up information.
Be concise and accurate in your responses.  Make it clear which answers are from general knowledge and which are from the provided content. List the files used to inform your answer."""
})
RAG_PROMPT_DIRECTIVE = "Remember to list the files you used to inform your answer."



DOCUMENT_TEMPLATE="""title: {filename} | text: {full_content}"""
SHINGLE_TEMPLATE="""title: {filename} | text: {shingle}"""
RETRIEVAL_TEMPLATE="""task: search result | query: {query}"""