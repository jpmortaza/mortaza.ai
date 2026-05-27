"""ScreenshotTool — tira screenshot via `screencapture` do macOS."""
from __future__ import annotations

import subprocess
import time
from typing import Literal

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError


class ScreenshotArgs(BaseModel):
    mode: Literal["full", "interactive", "window"] = Field(
        "full",
        description=(
            "Modo de captura: 'full' (tela inteira), 'interactive' (usuário seleciona "
            "área), 'window' (usuário clica numa janela)."
        ),
    )
    output_path: str | None = Field(
        None,
        description="Caminho do PNG de saída. Default: /tmp/mtzcode-shot-{timestamp}.png.",
    )


class ScreenshotTool(Tool):
    name = "screenshot"
    destructive = False
    description = (
        "Tira screenshot da tela inteira ou de uma área/janela no macOS."
    )
    Args = ScreenshotArgs

    def run(self, args: ScreenshotArgs) -> str:  # type: ignore[override]
        # Default: arquivo único em /tmp baseado no timestamp.
        path = args.output_path or f"/tmp/mtzcode-shot-{int(time.time())}.png"

        # Mapeia modo → flags do screencapture.
        if args.mode == "full":
            cmd = ["screencapture", "-x", path]
        elif args.mode == "interactive":
            cmd = ["screencapture", "-i", path]
        else:  # window
            cmd = ["screencapture", "-iW", path]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                # Modos interativos podem demorar até o usuário clicar.
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            raise ToolError("screencapture excedeu o timeout de 300s.") from None
        except FileNotFoundError as exc:
            raise ToolError(
                "screencapture não encontrado — esta tool só funciona no macOS."
            ) from exc
        except OSError as exc:
            raise ToolError(f"falha ao executar screencapture: {exc}") from exc

        if result.returncode != 0:
            raise ToolError(
                f"screencapture falhou (exit_code={result.returncode}): "
                f"{result.stderr.strip()}"
            )
        return f"salvo em {path}"
