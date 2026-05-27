"""SearchKnowledgeTool — busca semântica em knowledge bases permanentes."""
from __future__ import annotations

from pydantic import BaseModel, Field

from mtzcode.knowledge import list_knowledge_bases, search_knowledge_base
from mtzcode.tools.base import Tool, ToolError


class SearchKnowledgeArgs(BaseModel):
    query: str = Field(
        ...,
        description="Consulta em linguagem natural sobre o conteúdo da knowledge base.",
    )
    base: str = Field(
        ...,
        description=(
            "Nome da knowledge base a consultar (ex: 'empresa', 'projeto-x'). "
            "Use uma base que já tenha sido criada com `mtzcode knowledge add`."
        ),
    )
    top_k: int = Field(
        5, description="Quantos resultados retornar (1-20).", ge=1, le=20
    )


class SearchKnowledgeTool(Tool):
    name = "search_knowledge"
    description = (
        "Busca semântica em uma knowledge base permanente do usuário "
        "(documentos como PDFs, markdowns, textos de empresa/projeto). "
        "Use pra responder perguntas sobre o conteúdo desses documentos. "
        "Liste as bases disponíveis primeiro se não souber qual usar."
    )
    Args = SearchKnowledgeArgs

    def run(self, args: SearchKnowledgeArgs) -> str:  # type: ignore[override]
        try:
            hits = search_knowledge_base(args.base, args.query, top_k=args.top_k)
        except FileNotFoundError as exc:
            bases = list_knowledge_bases()
            if not bases:
                raise ToolError(
                    "nenhuma knowledge base encontrada. "
                    "Crie uma com `mtzcode knowledge add --name <nome> <pasta>`."
                ) from exc
            available = ", ".join(name for name, _, _ in bases)
            raise ToolError(
                f"{exc}. Disponíveis: {available}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"erro na busca: {exc}") from exc

        if not hits:
            return f"(nenhum resultado em '{args.base}' para '{args.query}')"

        parts: list[str] = []
        for i, h in enumerate(hits, start=1):
            snippet = h.content.strip()
            if len(snippet) > 800:
                snippet = snippet[:800] + "\n... (truncado)"
            parts.append(
                f"[{i}] {h.path}:{h.start_line}-{h.end_line}  "
                f"(score {h.score:.3f})\n{snippet}"
            )
        return "\n\n".join(parts)
