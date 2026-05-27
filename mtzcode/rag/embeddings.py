"""Cliente de embeddings — fala com Ollama via `/api/embed`.

Usa `nomic-embed-text` por padrão (modelo pequeno, rápido, boa qualidade).
"""
from __future__ import annotations

import os

import httpx
import numpy as np


class EmbeddingError(RuntimeError):
    pass


DEFAULT_EMBED_MODEL = os.getenv("MTZCODE_EMBED_MODEL", "nomic-embed-text")
DEFAULT_EMBED_HOST = os.getenv("MTZCODE_EMBED_HOST", "http://localhost:11434")


class EmbeddingClient:
    def __init__(
        self,
        host: str = DEFAULT_EMBED_HOST,
        model: str = DEFAULT_EMBED_MODEL,
        timeout_s: float = 120.0,
    ) -> None:
        self.host = host.rstrip("/")
        self.model = model
        self._client = httpx.Client(timeout=timeout_s)

    def embed(self, texts: list[str]) -> np.ndarray:
        """Retorna um array (N, D) de embeddings normalizados."""
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        try:
            response = self._client.post(
                f"{self.host}/api/embed",
                json={"model": self.model, "input": texts},
            )
        except httpx.HTTPError as exc:
            raise EmbeddingError(
                f"falha ao conectar no Ollama em {self.host}: {exc}"
            ) from exc

        if response.status_code != 200:
            raise EmbeddingError(
                f"Ollama respondeu {response.status_code}: {response.text[:500]}"
            )
        data = response.json()
        embeddings = data.get("embeddings")
        if not embeddings:
            raise EmbeddingError(f"resposta sem embeddings: {str(data)[:200]}")
        arr = np.asarray(embeddings, dtype=np.float32)
        # Normaliza pra cosine similarity virar dot product
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        return arr / norms

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "EmbeddingClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
