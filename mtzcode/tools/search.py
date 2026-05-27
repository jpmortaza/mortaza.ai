"""SearchCodeTool — busca semântica sobre o projeto indexado (RAG)."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from mtzcode.rag import EmbeddingClient, EmbeddingError, Index
from mtzcode.tools.base import Tool, ToolError


def _index_path() -> Path:
    """Local do índice: `<cwd>/.mtzcode/index.db`."""
    return Path.cwd() / ".mtzcode" / "index.db"


class SearchCodeArgs(BaseModel):
    query: str = Field(
        ...,
        description=(
            "Consulta em linguagem natural. Ex: 'onde a config é carregada', "
            "'função que faz parsing de tool calls', 'template do banner'."
        ),
    )
    top_k: int = Field(
        5,
        description="Quantos resultados retornar (1-20).",
        ge=1,
        le=20,
    )


class SearchCodeTool(Tool):
    name = "search_code"
    description = (
        "Busca semântica no código do projeto usando embeddings locais. "
        "Retorna os trechos mais relevantes com caminho e número de linha. "
        "Use pra descobrir onde algo está implementado sem saber o nome exato. "
        "Requer que o projeto tenha sido indexado antes (`mtzcode index` ou /indexar). "
        "Mais eficiente que grep pra perguntas conceituais."
    )
    Args = SearchCodeArgs

    def run(self, args: SearchCodeArgs) -> str:  # type: ignore[override]
        path = _index_path()
        if not path.exists():
            raise ToolError(
                f"índice não existe em {path}. "
                "Rode `mtzcode index` no terminal ou /indexar no REPL primeiro."
            )

        try:
            with EmbeddingClient() as embedder, Index(path) as idx:
                stats = idx.stats()
                if stats.total_chunks == 0:
                    raise ToolError(
                        "índice existe mas está vazio. Rode /indexar novamente."
                    )
                embedding = embedder.embed([args.query])
                if len(embedding) == 0:
                    raise ToolError("falha ao gerar embedding da query.")
                hits = idx.search(embedding[0], top_k=args.top_k)
        except EmbeddingError as exc:
            raise ToolError(
                f"erro no modelo de embeddings: {exc}. "
                "Verifique se `nomic-embed-text` está no Ollama."
            ) from exc

        if not hits:
            return f"(nenhum resultado para '{args.query}')"

        parts: list[str] = []
        for i, h in enumerate(hits, start=1):
            snippet = h.content.strip()
            if len(snippet) > 600:
                snippet = snippet[:600] + "\n... (truncado)"
            parts.append(
                f"[{i}] {h.path}:{h.start_line}-{h.end_line}  "
                f"(score {h.score:.3f})\n{snippet}"
            )
        return "\n\n".join(parts)
