"""Knowledge base do mtzcode — memória permanente de documentos.

Diferente do RAG de código (`mtzcode index`), a knowledge base indexa
**documentos** (PDF, Markdown, txt, docx) de uma pasta do usuário e cria
uma memória permanente que o agent pode consultar via a tool `search_knowledge`.

Cada knowledge base tem um nome (`empresa`, `projeto-x`, `receitas`, etc)
e é armazenada em `~/.mtzcode/knowledge/<nome>.db`.

Uso:
    mtzcode knowledge add --name empresa ~/docs/empresa
    mtzcode knowledge list
    mtzcode knowledge search "política de férias" --name empresa
    mtzcode knowledge remove empresa
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np

from mtzcode.rag.embeddings import EmbeddingClient
from mtzcode.rag.index import Hit, Index

# Extensões suportadas e como extrair texto delas
TEXT_EXTENSIONS = {
    # Texto puro e markdown
    ".md", ".txt", ".rst", ".mdx",
    # Configs / dados
    ".yaml", ".yml", ".toml", ".json", ".ini", ".cfg", ".csv",
    # Código (útil pra docs técnicas)
    ".py", ".js", ".ts", ".go", ".rs", ".java", ".sh",
    # HTML (vira texto puro na leitura)
    ".html", ".htm",
    # SQL
    ".sql",
}

# PDF e docx são tratados separadamente (se as libs estiverem disponíveis)
PDF_EXT = ".pdf"
DOCX_EXT = ".docx"

MAX_FILE_BYTES = 5_000_000  # 5 MB — PDFs podem ser grandes
CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200
EMBED_BATCH = 32


def knowledge_dir() -> Path:
    """Diretório raiz onde as knowledge bases ficam."""
    custom = os.getenv("MTZCODE_KNOWLEDGE_DIR")
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".mtzcode" / "knowledge"


def knowledge_db_path(name: str) -> Path:
    return knowledge_dir() / f"{name}.db"


def list_knowledge_bases() -> list[tuple[str, Path, int]]:
    """Retorna (name, path, size_bytes) pra cada knowledge base encontrada."""
    out: list[tuple[str, Path, int]] = []
    d = knowledge_dir()
    if not d.exists():
        return out
    for f in sorted(d.glob("*.db")):
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        out.append((f.stem, f, size))
    return out


@dataclass
class IngestStats:
    files_scanned: int = 0
    files_ingested: int = 0
    chunks_created: int = 0
    files_skipped: int = 0


def _extract_text(path: Path) -> str | None:
    """Extrai texto de um arquivo. Retorna None se não suportado/ilegível."""
    ext = path.suffix.lower()

    if ext in TEXT_EXTENSIONS:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    if ext == PDF_EXT:
        return _extract_pdf(path)

    if ext == DOCX_EXT:
        return _extract_docx(path)

    return None


def _extract_pdf(path: Path) -> str | None:
    """Extrai texto de PDF se pypdf estiver instalado. Senão retorna None."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(str(path))
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001
                continue
        return "\n\n".join(pages) if pages else None
    except Exception:  # noqa: BLE001
        return None


def _extract_docx(path: Path) -> str | None:
    """Extrai texto de .docx se python-docx estiver instalado."""
    try:
        import docx  # type: ignore
    except ImportError:
        return None
    try:
        doc = docx.Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    except Exception:  # noqa: BLE001
        return None


def _iter_supported_files(root: Path) -> Iterator[Path]:
    """Walk recursivo procurando arquivos suportados. Respeita excludes comuns."""
    excludes = {
        ".git", ".venv", "venv", "__pycache__", "node_modules",
        ".DS_Store", "dist", "build",
    }
    supported_exts = TEXT_EXTENSIONS | {PDF_EXT, DOCX_EXT}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in excludes for part in p.parts):
            continue
        if p.suffix.lower() in supported_exts:
            yield p


def _chunk_text(text: str) -> Iterator[tuple[int, int, str]]:
    """Chunks por janela de caracteres com overlap, quebrando em newlines
    próximas. Mesmo formato do rag/indexer.py."""
    if not text:
        return
    lines = text.splitlines()
    if not lines:
        return
    if len(text) <= CHUNK_CHARS:
        yield 1, len(lines), text
        return

    char_pos = 0
    while char_pos < len(text):
        end_pos = min(char_pos + CHUNK_CHARS, len(text))
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


def ingest_folder(
    name: str,
    folder: Path,
    embedder: EmbeddingClient,
    on_progress=None,
    clear_first: bool = False,
) -> IngestStats:
    """Ingesta uma pasta inteira na knowledge base `name`.

    - `clear_first=True` apaga a base antes (útil pra recomeçar do zero)
    - Se a base já existe, faz update incremental por mtime
    """
    db_path = knowledge_db_path(name)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    idx = Index(db_path)
    if clear_first:
        idx.clear()

    stats = IngestStats()

    try:
        files = list(_iter_supported_files(folder))
        seen_paths = {str(f.relative_to(folder)) for f in files}

        # Remove arquivos que sumiram da pasta
        cur = idx._conn.execute("SELECT DISTINCT path FROM chunks")
        indexed = {row[0] for row in cur.fetchall()}
        for stale in indexed - seen_paths:
            idx.delete_file(stale)

        for f in files:
            stats.files_scanned += 1
            rel = str(f.relative_to(folder))

            try:
                mtime = f.stat().st_mtime
                size = f.stat().st_size
            except OSError:
                stats.files_skipped += 1
                continue

            if size > MAX_FILE_BYTES:
                stats.files_skipped += 1
                continue

            existing_mtime = idx.file_mtime(rel)
            if existing_mtime is not None and existing_mtime >= mtime:
                continue

            text = _extract_text(f)
            if text is None or not text.strip():
                stats.files_skipped += 1
                continue

            chunks = list(_chunk_text(text))
            if not chunks:
                continue

            all_embeddings = []
            for i in range(0, len(chunks), EMBED_BATCH):
                batch = chunks[i : i + EMBED_BATCH]
                texts = [c[2] for c in batch]
                try:
                    embs = embedder.embed(texts)
                except Exception:  # noqa: BLE001
                    all_embeddings = []
                    break
                all_embeddings.append(embs)

            if not all_embeddings:
                stats.files_skipped += 1
                continue

            embeddings = np.concatenate(all_embeddings, axis=0)
            idx.delete_file(rel)
            idx.add_chunks(rel, chunks, embeddings, mtime)
            stats.files_ingested += 1
            stats.chunks_created += len(chunks)

            if on_progress is not None:
                on_progress(stats, rel)
    finally:
        idx.close()

    return stats


def search_knowledge_base(
    name: str, query: str, top_k: int = 5
) -> list[Hit]:
    """Busca numa knowledge base específica."""
    path = knowledge_db_path(name)
    if not path.exists():
        raise FileNotFoundError(f"knowledge base '{name}' não existe em {path}")

    with EmbeddingClient() as embedder, Index(path) as idx:
        embeddings = embedder.embed([query])
        if len(embeddings) == 0:
            return []
        return idx.search(embeddings[0], top_k=top_k)
