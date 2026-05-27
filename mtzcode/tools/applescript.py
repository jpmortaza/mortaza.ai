"""AppleScriptTool — executa AppleScript ou JXA via `osascript` no macOS."""
from __future__ import annotations

import subprocess
from typing import Literal

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError

# Limite de saída pra não estourar o contexto do modelo.
MAX_OUTPUT = 30_000


class AppleScriptArgs(BaseModel):
    script: str = Field(
        ..., description="Código AppleScript ou JXA a executar via osascript."
    )
    language: Literal["applescript", "jxa"] = Field(
        "applescript",
        description="Linguagem do script: 'applescript' (default) ou 'jxa' (JavaScript for Automation).",
    )
    timeout: int = Field(
        30,
        description="Timeout em segundos (default 30, máx 600).",
        ge=1,
        le=600,
    )


class AppleScriptTool(Tool):
    name = "applescript"
    # Marcado como destrutivo: pode mexer em apps nativos, enviar emails, etc.
    destructive = True
    description = (
        "Executa AppleScript ou JXA no macOS. Dá acesso a Mail, Calendar, Notes, "
        "Reminders, Messages, Safari, Finder, Music, System Events. Use para automação "
        "nativa do mac. Exemplos: criar evento no Calendar, enviar email, ler notas."
    )
    Args = AppleScriptArgs

    def run(self, args: AppleScriptArgs) -> str:  # type: ignore[override]
        # Mapeia o nome amigável pro flag aceito pelo osascript.
        lang_flag = "AppleScript" if args.language == "applescript" else "JavaScript"
        try:
            result = subprocess.run(
                ["osascript", "-l", lang_flag, "-e", args.script],
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(
                f"script excedeu o timeout de {args.timeout}s e foi abortado."
            ) from None
        except FileNotFoundError as exc:
            raise ToolError(
                "osascript não encontrado — esta tool só funciona no macOS."
            ) from exc
        except OSError as exc:
            raise ToolError(f"falha ao executar osascript: {exc}") from exc

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
    """Trunca a saída pra evitar respostas gigantes."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({label} truncado em {limit} chars)"
