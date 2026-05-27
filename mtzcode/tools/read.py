"""ReadTool — lê o conteúdo de um arquivo de texto."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError

MAX_BYTES = 200_000  # ~200 KB; arquivos maiores precisam de offset/limit


class ReadArgs(BaseModel):
    path: str = Field(..., description="Caminho absoluto ou relativo do arquivo a ler.")
    offset: int = Field(0, description="Linha inicial (1-based). 0 = início.", ge=0)
    limit: int = Field(0, description="Número máximo de linhas (0 = todas).", ge=0)


class ReadTool(Tool):
    name = "read"
    description = (
        "Lê o conteúdo de um arquivo de texto e retorna com numeração de linhas. "
        "Use sempre antes de editar um arquivo, para conhecer o conteúdo exato. "
        "Use offset/limit para arquivos grandes."
    )
    Args = ReadArgs

    def run(self, args: ReadArgs) -> str:  # type: ignore[override]
        p = Path(args.path).expanduser()
        if not p.exists():
            raise ToolError(f"arquivo não existe: {p}")
        if not p.is_file():
            raise ToolError(f"não é um arquivo: {p}")
        if p.stat().st_size > MAX_BYTES and args.limit == 0:
            raise ToolError(
                f"arquivo grande ({p.stat().st_size} bytes). "
                f"Use limit/offset para ler em pedaços."
            )

        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"erro lendo {p}: {exc}") from exc

        lines = text.splitlines()
        start = max(args.offset - 1, 0) if args.offset else 0
        end = start + args.limit if args.limit else len(lines)
        slice_ = lines[start:end]

        out = []
        for i, line in enumerate(slice_, start=start + 1):
            out.append(f"{i:>6}\t{line}")
        if not out:
            return f"(arquivo vazio: {p})"
        return "\n".join(out)
