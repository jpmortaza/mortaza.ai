"""OpenAppTool — abre apps ou arquivos no macOS via `open`."""
from __future__ import annotations

import subprocess

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError


class OpenAppArgs(BaseModel):
    target: str = Field(
        ...,
        description=(
            "Alvo a abrir: nome do app (ex: 'Safari'), caminho de arquivo ou URL."
        ),
    )
    app: str | None = Field(
        None,
        description=(
            "Nome do app pra forçar com qual app o target será aberto "
            "(equivalente a `open -a <app> <target>`)."
        ),
    )


class OpenAppTool(Tool):
    name = "open_app"
    destructive = False
    description = (
        "Abre um aplicativo do macOS pelo nome (ex: Safari, Notes, Mail, Calendar) "
        "ou um arquivo no app padrão."
    )
    Args = OpenAppArgs

    def run(self, args: OpenAppArgs) -> str:  # type: ignore[override]
        # Se app foi informado, força a abertura nele; senão usa o handler default.
        if args.app:
            cmd = ["open", "-a", args.app, args.target]
        else:
            cmd = ["open", args.target]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except FileNotFoundError as exc:
            raise ToolError(
                "comando `open` não encontrado — esta tool só funciona no macOS."
            ) from exc
        except OSError as exc:
            raise ToolError(f"falha ao executar open: {exc}") from exc

        if result.returncode != 0:
            raise ToolError(
                f"open falhou (exit_code={result.returncode}): {result.stderr.strip()}"
            )
        if args.app:
            return f"abrindo `{args.target}` com {args.app}"
        return f"abrindo `{args.target}`"
