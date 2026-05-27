"""OpenUrlTool — abre uma URL no navegador padrão do sistema."""
from __future__ import annotations

import webbrowser

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError


class OpenUrlArgs(BaseModel):
    url: str = Field(..., description="URL a abrir no navegador padrão do usuário.")


class OpenUrlTool(Tool):
    name = "open_url"
    destructive = False
    description = (
        "Abre uma URL no navegador padrão do usuário. "
        "Use quando quiser mostrar algo visualmente ao usuário."
    )
    Args = OpenUrlArgs

    def run(self, args: OpenUrlArgs) -> str:  # type: ignore[override]
        # webbrowser.open devolve False se não conseguir despachar pro SO.
        try:
            ok = webbrowser.open(args.url)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"falha ao abrir url: {exc}") from exc
        if not ok:
            raise ToolError("não foi possível abrir a url no navegador padrão.")
        return f"abrindo {args.url} no navegador"
