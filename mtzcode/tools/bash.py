"""BashTool — executa comandos shell e captura stdout/stderr."""
from __future__ import annotations

import subprocess

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError

DEFAULT_TIMEOUT = 60
MAX_OUTPUT = 30_000  # caracteres por stream


class BashArgs(BaseModel):
    command: str = Field(..., description="Comando shell a executar (via /bin/bash -c).")
    timeout: int = Field(
        DEFAULT_TIMEOUT,
        description=f"Timeout em segundos (default {DEFAULT_TIMEOUT}, máx 600).",
        ge=1,
        le=600,
    )
    cwd: str | None = Field(
        None, description="Diretório de trabalho. Default: cwd do mtzcode."
    )


class BashTool(Tool):
    name = "bash"
    destructive = True
    description = (
        "Executa um comando shell no sistema do usuário e retorna stdout/stderr/exit_code. "
        "Use para listar arquivos, rodar testes, git, build, etc. "
        "Não use para criar/editar arquivos — use write/edit. "
        "Cuidado com comandos destrutivos (rm -rf, etc)."
    )
    Args = BashArgs

    def run(self, args: BashArgs) -> str:  # type: ignore[override]
        try:
            result = subprocess.run(
                args.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=args.timeout,
                cwd=args.cwd,
                executable="/bin/bash",
            )
        except subprocess.TimeoutExpired:
            raise ToolError(
                f"comando excedeu o timeout de {args.timeout}s e foi abortado."
            ) from None
        except OSError as exc:
            raise ToolError(f"falha ao executar comando: {exc}") from exc

        stdout = _truncate(result.stdout, MAX_OUTPUT, "stdout")
        stderr = _truncate(result.stderr, MAX_OUTPUT, "stderr")
        parts = [f"exit_code: {result.returncode}"]
        if stdout:
            parts.append(f"--- stdout ---\n{stdout}")
        if stderr:
            parts.append(f"--- stderr ---\n{stderr}")
        if not stdout and not stderr:
            parts.append("(sem saída)")
        return "\n".join(parts)


def _truncate(text: str, limit: int, label: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({label} truncado em {limit} chars)"
