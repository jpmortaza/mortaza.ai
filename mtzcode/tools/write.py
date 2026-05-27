"""WriteTool — cria ou sobrescreve completamente um arquivo."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError


class WriteArgs(BaseModel):
    path: str = Field(..., description="Caminho do arquivo a criar/sobrescrever.")
    content: str = Field(..., description="Conteúdo completo do arquivo.")


class WriteTool(Tool):
    name = "write"
    destructive = True
    description = (
        "Cria um arquivo novo ou substitui completamente o conteúdo de um existente. "
        "Use SOMENTE para arquivos novos ou rewrites totais. "
        "Para alterar parte de um arquivo, prefira a tool `edit`. "
        "Antes de sobrescrever um arquivo existente, leia ele primeiro com `read`."
    )
    Args = WriteArgs

    def run(self, args: WriteArgs) -> str:  # type: ignore[override]
        p = Path(args.path).expanduser()
        existed = p.exists()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args.content, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"erro escrevendo {p}: {exc}") from exc

        verb = "sobrescrito" if existed else "criado"
        n_lines = args.content.count("\n") + (0 if args.content.endswith("\n") else 1)
        return f"arquivo {verb}: {p} ({len(args.content)} bytes, {n_lines} linhas)"
