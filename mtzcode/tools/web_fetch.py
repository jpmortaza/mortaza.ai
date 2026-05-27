"""WebFetchTool — baixa uma URL e devolve seu conteúdo (markdown/texto/HTML)."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, HttpUrl

from mtzcode.tools.base import Tool, ToolError

# Timeout padrão das requisições HTTP, em segundos.
DEFAULT_TIMEOUT = 20.0
# User-agent enviado nas requisições.
USER_AGENT = "mtzcode/0.1"


class WebFetchArgs(BaseModel):
    url: HttpUrl = Field(..., description="URL completa (http/https) a baixar.")
    max_chars: int = Field(
        8000,
        description="Limite de caracteres do conteúdo retornado (default 8000).",
        ge=100,
        le=200_000,
    )
    raw: bool = Field(
        False,
        description="Se True, devolve o HTML bruto sem converter para markdown.",
    )


class WebFetchTool(Tool):
    name = "web_fetch"
    destructive = False
    description = (
        "Baixa uma URL e retorna o conteúdo convertido para markdown/texto. "
        "Use para ler documentação, artigos, páginas. "
        "Não use para pesquisar (use web_search)."
    )
    Args = WebFetchArgs

    def run(self, args: WebFetchArgs) -> str:  # type: ignore[override]
        # Import lazy do httpx pra não pesar o boot da CLI.
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise ToolError(
                "dependência ausente: instale `httpx` para usar web_fetch."
            ) from exc

        url = str(args.url)
        # Pydantic HttpUrl já valida scheme, mas reforçamos pra rejeitar
        # data:, javascript:, file: caso alguém burle o cast.
        if not url.lower().startswith(("http://", "https://")):
            raise ToolError(f"scheme não suportado: {url} (use http/https)")

        try:
            response = httpx.get(
                url,
                timeout=DEFAULT_TIMEOUT,
                follow_redirects=True,
                max_redirects=5,
                headers={"User-Agent": USER_AGENT},
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"falha ao baixar {url}: {exc}") from exc

        # Valida URL final (após redirects) — não pode ter sido pra esquema esquisito.
        final_url = str(response.url)
        if not final_url.lower().startswith(("http://", "https://")):
            raise ToolError(f"redirect levou a scheme inseguro: {final_url}")

        content_type = response.headers.get("content-type", "").lower()
        status = response.status_code

        # Decide o corpo: HTML vira markdown (a menos que raw=True), resto vai como texto.
        body = response.text
        if not args.raw and "html" in content_type:
            body = _html_to_markdown(body)

        body = _truncate(body, args.max_chars)

        header = (
            f"url: {final_url}\n"
            f"status: {status}\n"
            f"content-type: {content_type or '(desconhecido)'}\n"
            f"--- conteúdo ---\n"
        )
        return header + body


def _html_to_markdown(html: str) -> str:
    """Converte HTML em markdown via html2text; cai para regex se a lib faltar."""
    try:
        import html2text  # type: ignore

        converter = html2text.HTML2Text()
        converter.ignore_images = True
        converter.ignore_emphasis = False
        converter.body_width = 0
        return converter.handle(html).strip()
    except ImportError:
        return _strip_tags(html)


def _strip_tags(html: str) -> str:
    """Fallback bem simples: remove script/style e tags, normaliza espaços."""
    # Remove blocos de script/style inteiros.
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    # Remove comentários HTML.
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)
    # Remove qualquer tag restante.
    text = re.sub(r"<[^>]+>", " ", html)
    # Decodifica algumas entidades comuns.
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    # Colapsa espaços/quebras de linha.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    return text.strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncado em {limit} chars)"
