"""PythonExecTool — roda código Python num subprocess curto.

Diferente do `bash`, essa tool é dedicada a executar trechos de Python
ad-hoc (cálculos, parsing, validação de uma lib, geração de dados, teste
rápido de uma função). Usa o mesmo Python do mtzcode (sys.executable),
então tem acesso a todas as libs já instaladas no .venv. Roda em
subprocess pra isolar exceções e aplicar timeout.

Quando usar:
  - Validar uma hipótese ("o que essa regex retorna pra esse input?")
  - Calcular algo ("quantos arquivos .py têm mais de 200 linhas?")
  - Gerar conteúdo programaticamente ("crie 50 lorem ipsum em JSONL")
  - Testar uma função sem precisar criar arquivo + rodar via bash

Não use pra:
  - Operações de filesystem que já tem tool dedicada (use read/write/edit/glob)
  - Comandos shell (use bash)
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError

DEFAULT_TIMEOUT = 30
MAX_OUTPUT = 20_000


class PythonExecArgs(BaseModel):
    code: str = Field(
        ...,
        description=(
            "Código Python a executar. Use print() pra ver resultados — "
            "stdout volta como resposta. Aceita múltiplas linhas."
        ),
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT,
        description=f"Timeout em segundos (default {DEFAULT_TIMEOUT}, máx 300).",
        ge=1,
        le=300,
    )
    cwd: str | None = Field(
        None, description="Diretório de trabalho. Default: cwd do mtzcode."
    )


class PythonExecTool(Tool):
    name = "python_exec"
    destructive = False  # leitura por padrão; modelo evita rm/etc
    description = (
        "Executa código Python num subprocess curto e retorna stdout/stderr. "
        "Use pra validar hipóteses, calcular algo rápido, testar uma função "
        "ou gerar conteúdo programaticamente sem precisar criar arquivo. "
        "Use print() pra ver resultados. Roda no Python do mtzcode (mesmo .venv)."
    )
    Args = PythonExecArgs

    def run(self, args: PythonExecArgs) -> str:  # type: ignore[override]
        # Dedent caso o modelo mande indentado
        code = textwrap.dedent(args.code).strip()
        if not code:
            raise ToolError("código vazio.")
        try:
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=args.timeout,
                cwd=args.cwd,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(
                f"código excedeu timeout de {args.timeout}s. Otimize ou aumente o timeout."
            ) from None
        except OSError as exc:
            raise ToolError(f"falha ao executar python: {exc}") from exc

        stdout = _truncate(result.stdout, MAX_OUTPUT, "stdout")
        stderr = _truncate(result.stderr, MAX_OUTPUT, "stderr")
        parts = [f"exit_code: {result.returncode}"]
        if stdout:
            parts.append(f"--- stdout ---\n{stdout}")
        if stderr:
            parts.append(f"--- stderr ---\n{stderr}")
        if not stdout and not stderr:
            parts.append("(sem saída — use print() pra ver resultados)")
        return "\n".join(parts)


def _truncate(text: str, limit: int, label: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({label} truncado em {limit} chars)"
