"""Indexer — varre um projeto, chunka os arquivos, gera embeddings e persiste.

Respeita `.gitignore` via git ls-files se for um repo; senão usa uma lista
padrão de excludes. Só indexa arquivos de texto (detecção heurística).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from mtzcode.rag.embeddings import EmbeddingClient
from mtzcode.rag.index import Index

DEFAULT_EXCLUDES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "target",
    ".DS_Store",
}

TEXT_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
    ".c", ".h", ".cpp", ".hpp", ".rb", ".php", ".sh", ".bash", ".zsh",
    ".md", ".rst", ".txt", ".yaml", ".yml", ".toml", ".json", ".ini",
    ".cfg", ".html", ".css", ".scss", ".sql", ".lua", ".vim", ".el",
    ".swift", ".dart", ".ex", ".exs", ".erl", ".clj", ".cljs", ".sc",
    ".scala", ".ml", ".mli", ".hs", ".fs", ".fsi", ".proto",
}

MAX_FILE_BYTES = 500_000  # 500 KB — pula arquivos enormes
CHUNK_CHARS = 1500        # ~300-400 tokens, bom pro nomic-embed
CHUNK_OVERLAP = 200
EMBED_BATCH = 32


@dataclass
class IndexProgress:
    files_scanned: int
    files_indexed: int
    chunks_created: int
    files_skipped: int


class ProjectIndexer:
    def __init__(
        self,
        root: Path,
        index: Index,
        embedder: EmbeddingClient,
    ) -> None:
        self.root = root.resolve()
        self.index = index
        self.embedder = embedder

    def index_project(self, on_progress=None) -> IndexProgress:
        """Varre o projeto e indexa. Atualiza arquivos que mudaram (por mtime).
        Remove do índice arquivos que não existem mais.
        """
        progress = IndexProgress(0, 0, 0, 0)
        files = list(self._iter_files())

        # Remove do índice arquivos que não existem mais
        # (conservador: só limpa os que sumiram de fato)
        seen_paths = {str(f.relative_to(self.root)) for f in files}
        cur = self.index._conn.execute("SELECT DISTINCT path FROM chunks")
        indexed_paths = {row[0] for row in cur.fetchall()}
        for stale in indexed_paths - seen_paths:
            self.index.delete_file(stale)

        # Reindexa arquivos novos/modificados
        for f in files:
            progress.files_scanned += 1
            rel = str(f.relative_to(self.root))
            try:
                mtime = f.stat().st_mtime
                size = f.stat().st_size
            except OSError:
                progress.files_skipped += 1
                continue

            if size > MAX_FILE_BYTES:
                progress.files_skipped += 1
                continue

            existing_mtime = self.index.file_mtime(rel)
            if existing_mtime is not None and existing_mtime >= mtime:
                continue  # já indexado e sem mudanças

            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                progress.files_skipped += 1
                continue

            chunks = list(_chunk_text(text))
            if not chunks:
                continue

            # Gera embeddings em batches
            all_embeddings = []
            for i in range(0, len(chunks), EMBED_BATCH):
                batch = chunks[i : i + EMBED_BATCH]
                texts = [c[2] for c in batch]
                try:
                    embs = self.embedder.embed(texts)
                except Exception:
                    progress.files_skipped += 1
                    all_embeddings = []
                    break
                all_embeddings.append(embs)

            if not all_embeddings:
                continue

            import numpy as np
            embeddings = np.concatenate(all_embeddings, axis=0)

            self.index.delete_file(rel)
            self.index.add_chunks(rel, chunks, embeddings, mtime)
            progress.files_indexed += 1
            progress.chunks_created += len(chunks)

            if on_progress is not None:
                on_progress(progress, rel)

        return progress

    def _iter_files(self) -> Iterator[Path]:
        """Itera sobre arquivos candidatos. Usa `git ls-files` se for repo,
        senão faz walk manual respeitando DEFAULT_EXCLUDES."""
        if (self.root / ".git").exists():
            try:
                result = subprocess.run(
                    ["git", "-C", str(self.root), "ls-files"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        p = self.root / line.strip()
                        if p.is_file() and _is_text_file(p):
                            yield p
                    return
            except (subprocess.SubprocessError, OSError):
                pass

        # Fallback: walk manual
        for p in self.root.rglob("*"):
            if not p.is_file():
                continue
            if any(part in DEFAULT_EXCLUDES for part in p.parts):
                continue
            if _is_text_file(p):
                yield p


def _is_text_file(p: Path) -> bool:
    return p.suffix.lower() in TEXT_EXTENSIONS


def _chunk_text(text: str) -> Iterator[tuple[int, int, str]]:
    """Divide texto em chunks com overlap. Retorna (start_line, end_line, content).

    Estratégia simples: por janela de caracteres com quebra em newlines próxima.
    Linhas são 1-based pra casar com a convenção de editores.
    """
    if not text:
        return
    lines = text.splitlines()
    if not lines:
        return

    # Se o arquivo é pequeno, um único chunk
    if len(text) <= CHUNK_CHARS:
        yield 1, len(lines), text
        return

    # Janelas de CHUNK_CHARS com overlap
    char_pos = 0
    while char_pos < len(text):
        end_pos = min(char_pos + CHUNK_CHARS, len(text))
        # Encontra quebra de linha mais próxima pra não cortar no meio
        if end_pos < len(text):
            nl = text.rfind("\n", char_pos + CHUNK_CHARS // 2, end_pos)
            if nl != -1:
                end_pos = nl + 1

        chunk = text[char_pos:end_pos]
        if chunk.strip():
            start_line = text.count("\n", 0, char_pos) + 1
            end_line = text.count("\n", 0, end_pos) + 1
            yield start_line, end_line, chunk

        next_pos = end_pos - CHUNK_OVERLAP
        if next_pos <= char_pos:
            next_pos = end_pos
        char_pos = next_pos
