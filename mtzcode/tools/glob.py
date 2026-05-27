"""GlobTool — busca arquivos por padrão glob."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError

MAX_RESULTS = 500


class GlobArgs(BaseModel):
    pattern: str = Field(
        ...,
        description='Padrão glob, ex: "**/*.py", "src/**/*.ts". Suporta recursão com **.',
    )
    path: str = Field(".", description="Diretório raiz da busca. Default: cwd.")


class GlobTool(Tool):
    name = "glob"
    description = (
        "Busca arquivos por padrão glob, retornando caminhos relativos ordenados. "
        "Use para descobrir arquivos por nome ou extensão. "
        "Para buscar conteúdo dentro de arquivos, use `grep`."
    )
    Args = GlobArgs

    def run(self, args: GlobArgs) -> str:  # type: ignore[override]
        root = Path(args.path).expanduser()
        if not root.exists():
            raise ToolError(f"diretório não existe: {root}")
        if not root.is_dir():
            raise ToolError(f"não é um diretório: {root}")

        try:
            matches = sorted(root.glob(args.pattern))
        except (ValueError, OSError) as exc:
            raise ToolError(f"erro no glob: {exc}") from exc

        # filtra só arquivos (mais útil que dirs)
        files = [m for m in matches if m.is_file()]
        if not files:
            return f"(nenhum arquivo casou com '{args.pattern}' em {root})"

        truncated = False
        if len(files) > MAX_RESULTS:
            files = files[:MAX_RESULTS]
            truncated = True

        rel = [str(f.relative_to(root) if f.is_relative_to(root) else f) for f in files]
        out = "\n".join(rel)
        if truncated:
            out += f"\n... (truncado em {MAX_RESULTS} resultados)"
        return out
