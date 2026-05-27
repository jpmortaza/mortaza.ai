"""Índice vetorial persistente em SQLite.

Schema:
  chunks(id, path, start_line, end_line, content, embedding_blob, mtime)

Busca usa cosine similarity via produto escalar de vetores já normalizados.
Carregamos todos os embeddings em memória na query — OK pra projetos
de até dezenas de milhares de chunks.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class IndexStats:
    total_chunks: int
    total_files: int
    db_size_bytes: int


@dataclass
class Hit:
    path: str
    start_line: int
    end_line: int
    content: str
    score: float


class Index:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                content TEXT NOT NULL,
                embedding BLOB NOT NULL,
                mtime REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS chunks_path_idx ON chunks(path)"
        )
        self._conn.commit()

    # ---------------- write paths ----------------
    def delete_file(self, path: str) -> None:
        self._conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
        self._conn.commit()

    def add_chunks(
        self,
        path: str,
        chunks: list[tuple[int, int, str]],
        embeddings: np.ndarray,
        mtime: float,
    ) -> None:
        """chunks = [(start_line, end_line, content), ...]"""
        if len(chunks) != len(embeddings):
            raise ValueError("chunks e embeddings com tamanhos diferentes")
        rows = [
            (path, s, e, c, emb.astype(np.float32).tobytes(), mtime)
            for (s, e, c), emb in zip(chunks, embeddings)
        ]
        self._conn.executemany(
            "INSERT INTO chunks(path, start_line, end_line, content, embedding, mtime)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    def file_mtime(self, path: str) -> float | None:
        cur = self._conn.execute(
            "SELECT mtime FROM chunks WHERE path = ? LIMIT 1", (path,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def clear(self) -> None:
        self._conn.execute("DELETE FROM chunks")
        self._conn.commit()

    # ---------------- read paths ----------------
    def stats(self) -> IndexStats:
        cur = self._conn.execute("SELECT COUNT(*), COUNT(DISTINCT path) FROM chunks")
        row = cur.fetchone() or (0, 0)
        size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return IndexStats(total_chunks=row[0], total_files=row[1], db_size_bytes=size)

    def search(self, query_embedding: np.ndarray, top_k: int = 5) -> list[Hit]:
        """Retorna os top_k chunks com maior cosine similarity."""
        # Query já deve estar normalizada (shape: (D,) ou (1, D))
        q = np.asarray(query_embedding, dtype=np.float32).reshape(-1)

        cur = self._conn.execute(
            "SELECT id, path, start_line, end_line, content, embedding FROM chunks"
        )
        rows = cur.fetchall()
        if not rows:
            return []

        # Monta matriz (N, D) de embeddings
        embs = np.stack(
            [np.frombuffer(r[5], dtype=np.float32) for r in rows]
        )
        # Produto escalar = cosine sim (ambos normalizados)
        scores = embs @ q
        top_idx = np.argsort(-scores)[:top_k]

        hits: list[Hit] = []
        for i in top_idx:
            r = rows[int(i)]
            hits.append(
                Hit(
                    path=r[1],
                    start_line=r[2],
                    end_line=r[3],
                    content=r[4],
                    score=float(scores[int(i)]),
                )
            )
        return hits

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Index":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
