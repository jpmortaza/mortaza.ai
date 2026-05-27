"""Tools de clipboard do macOS — leitura via pbpaste e escrita via pbcopy."""
from __future__ import annotations

import subprocess

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError


class ClipboardReadArgs(BaseModel):
    """Sem argumentos — só lê o que tá no clipboard."""

    pass


class ClipboardReadTool(Tool):
    name = "clipboard_read"
    destructive = False
    description = (
        "Lê o conteúdo atual do clipboard (área de transferência) do macOS via pbpaste."
    )
    Args = ClipboardReadArgs

    def run(self, args: ClipboardReadArgs) -> str:  # type: ignore[override]
        try:
            result = subprocess.run(
                ["pbpaste"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise ToolError(
                "pbpaste não encontrado — esta tool só funciona no macOS."
            ) from exc
        except OSError as exc:
            raise ToolError(f"falha ao executar pbpaste: {exc}") from exc

        if result.returncode != 0:
            raise ToolError(
                f"pbpaste falhou (exit_code={result.returncode}): {result.stderr.strip()}"
            )
        return result.stdout


class ClipboardWriteArgs(BaseModel):
    text: str = Field(..., description="Texto a copiar pro clipboard do macOS.")


class ClipboardWriteTool(Tool):
    name = "clipboard_write"
    destructive = False
    description = (
        "Escreve um texto no clipboard (área de transferência) do macOS via pbcopy. "
        "Sobrescreve o conteúdo atual."
    )
    Args = ClipboardWriteArgs

    def run(self, args: ClipboardWriteArgs) -> str:  # type: ignore[override]
        try:
            result = subprocess.run(
                ["pbcopy"],
                input=args.text,
                text=True,
                capture_output=True,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise ToolError(
                "pbcopy não encontrado — esta tool só funciona no macOS."
            ) from exc
        except OSError as exc:
            raise ToolError(f"falha ao executar pbcopy: {exc}") from exc

        if result.returncode != 0:
            raise ToolError(
                f"pbcopy falhou (exit_code={result.returncode}): {result.stderr.strip()}"
            )
        return f"copiado {len(args.text)} chars pro clipboard"
