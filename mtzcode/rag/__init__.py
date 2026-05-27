"""RAG (Retrieval-Augmented Generation) do mtzcode.

Indexa arquivos do projeto com embeddings locais (via Ollama) e armazena
em SQLite. O agent pode buscar pedaços relevantes via a tool `search_code`.
"""
from mtzcode.rag.embeddings import EmbeddingClient, EmbeddingError
from mtzcode.rag.index import Index, IndexStats
from mtzcode.rag.indexer import ProjectIndexer

__all__ = [
    "EmbeddingClient",
    "EmbeddingError",
    "Index",
    "IndexStats",
    "ProjectIndexer",
]
