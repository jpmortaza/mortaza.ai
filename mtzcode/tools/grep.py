"""GrepTool — busca conteúdo em arquivos usando ripgrep (rg) se disponível."""
from __future__ import annotations

import shutil
import subprocess

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError

MAX_OUTPUT = 30_000


class GrepArgs(BaseModel):
    pattern: str = Field(..., description="Regex (estilo ripgrep/PCRE) a procurar.")
    path: str = Field(".", description="Arquivo ou diretório a buscar. Default: cwd.")
    glob: str | None = Field(
        None, description='Filtro de arquivos, ex: "*.py". Default: todos.'
    )
    case_insensitive: bool = Field(False, description="Busca case-insensitive (-i).")
    files_only: bool = Field(
        False, description="Retornar só os caminhos dos arquivos, sem as linhas."
    )
    context: int = Field(0, description="Linhas de contexto antes/depois (-C).", ge=0, le=10)


class GrepTool(Tool):
    name = "grep"
    description = (
        "Busca um padrão regex dentro de arquivos. Usa ripgrep (rg) se disponível, "
        "senão cai pro grep tradicional. Suporta filtro por glob, case-insensitive, "
        "contexto, e modo 'só nomes de arquivos'."
    )
    Args = GrepArgs

    def run(self, args: GrepArgs) -> str:  # type: ignore[override]
        rg = shutil.which("rg")
        if rg:
            cmd = [rg, "--color=never", "--no-heading", "--with-filename", "--line-number"]
            if args.case_insensitive:
                cmd.append("-i")
            if args.files_only:
                cmd.extend(["-l"])
            if args.context:
                cmd.extend(["-C", str(args.context)])
            if args.glob:
                cmd.extend(["--glob", args.glob])
            cmd.append(args.pattern)
            cmd.append(args.path)
        else:
            cmd = ["grep", "-rn"]
            if args.case_insensitive:
                cmd.append("-i")
            if args.files_only:
                cmd.append("-l")
            if args.context:
                cmd.extend(["-C", str(args.context)])
            if args.glob:
                cmd.extend(["--include", args.glob])
            cmd.append("-e")
            cmd.append(args.pattern)
            cmd.append(args.path)

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
        except subprocess.TimeoutExpired:
            raise ToolError("grep excedeu 30s — refine o padrão ou o caminho.") from None
        except OSError as exc:
            raise ToolError(f"erro executando grep: {exc}") from exc

        # rg/grep retornam exit 1 quando não acha — não é erro
        if result.returncode not in (0, 1):
            stderr = result.stderr.strip() or "(sem stderr)"
            raise ToolError(f"grep falhou (exit {result.returncode}): {stderr}")

        out = result.stdout
        if not out.strip():
            return f"(nenhum match para '{args.pattern}')"
        if len(out) > MAX_OUTPUT:
            out = out[:MAX_OUTPUT] + f"\n... (truncado em {MAX_OUTPUT} chars)"
        return out
