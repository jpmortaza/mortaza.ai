"""WebSearchTool — pesquisa na web via SearXNG (se configurado) ou DuckDuckGo HTML."""
from __future__ import annotations

import os
import re
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError

# Timeout das requisições de busca.
DEFAULT_TIMEOUT = 20.0
# User-agent realista pra evitar bloqueios do DuckDuckGo HTML.
REALISTIC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


class WebSearchArgs(BaseModel):
    query: str = Field(..., description="Termo a pesquisar.")
    num_results: int = Field(
        5,
        description="Número de resultados a retornar (1 a 20, default 5).",
        ge=1,
        le=20,
    )
    lang: str = Field(
        "pt-br",
        description="Idioma/região da busca (default pt-br).",
    )


class WebSearchTool(Tool):
    name = "web_search"
    destructive = False
    description = (
        "Pesquisa na web e retorna uma lista de resultados com título, URL e snippet. "
        "Use antes de web_fetch quando não souber a URL exata."
    )
    Args = WebSearchArgs

    def run(self, args: WebSearchArgs) -> str:  # type: ignore[override]
        # Import lazy do httpx.
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise ToolError(
                "dependência ausente: instale `httpx` para usar web_search."
            ) from exc

        results: list[dict[str, str]] = []
        errors: list[str] = []

        # Backend 1: SearXNG, se a env var estiver setada.
        searxng_url = os.environ.get("SEARXNG_URL", "").strip()
        if searxng_url:
            try:
                results = _searxng(httpx, searxng_url, args)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"searxng: {exc}")

        # Backend 2: DuckDuckGo HTML como fallback.
        if not results:
            try:
                results = _duckduckgo(httpx, args)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"duckduckgo: {exc}")

        if not results:
            joined = "; ".join(errors) if errors else "nenhum resultado"
            raise ToolError(f"falha ao pesquisar `{args.query}`: {joined}")

        return _format_results(results[: args.num_results])


def _searxng(httpx_mod, base_url: str, args: WebSearchArgs) -> list[dict[str, str]]:
    """Consulta uma instância SearXNG via POST /search com format=json."""
    url = base_url.rstrip("/") + "/search"
    data = {
        "q": args.query,
        "format": "json",
        "language": args.lang,
    }
    response = httpx_mod.post(
        url,
        data=data,
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": REALISTIC_UA, "Accept": "application/json"},
    )
    response.raise_for_status()
    payload = response.json()
    out: list[dict[str, str]] = []
    for item in payload.get("results", []):
        url = (item.get("url") or "").strip()
        if not _is_safe_http_url(url):
            continue
        out.append(
            {
                "title": (item.get("title") or "").strip(),
                "url": url,
                "snippet": (item.get("content") or "").strip(),
            }
        )
    return out


def _duckduckgo(httpx_mod, args: WebSearchArgs) -> list[dict[str, str]]:
    """Faz scrape da página HTML do DuckDuckGo (sem API key)."""
    response = httpx_mod.get(
        "https://html.duckduckgo.com/html/",
        params={"q": args.query, "kl": args.lang},
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        headers={
            "User-Agent": REALISTIC_UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": f"{args.lang},en;q=0.5",
        },
    )
    response.raise_for_status()
    return _parse_ddg_html(response.text)


def _parse_ddg_html(html: str) -> list[dict[str, str]]:
    """Tenta usar BeautifulSoup; se faltar, cai num parser de regex."""
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html, "html.parser")
        results: list[dict[str, str]] = []
        for block in soup.select("div.result"):
            a = block.select_one("a.result__a")
            snippet_el = block.select_one("a.result__snippet, .result__snippet")
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            url = _clean_ddg_url(a.get("href", ""))
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            if title and url:
                results.append({"title": title, "url": url, "snippet": snippet})
        return results
    except ImportError:
        return _regex_ddg(html)


def _regex_ddg(html: str) -> list[dict[str, str]]:
    """Fallback de parsing via regex pra resultados do DuckDuckGo HTML."""
    results: list[dict[str, str]] = []
    # Cada resultado é um <div class="result"> ... </div>; pegamos o link e o snippet.
    block_re = re.compile(r'<div class="result[^"]*">(.*?)</div>\s*</div>', re.DOTALL)
    link_re = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL
    )
    snippet_re = re.compile(
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL
    )
    for block in block_re.findall(html):
        link = link_re.search(block)
        if not link:
            continue
        href = _clean_ddg_url(unescape(link.group(1)))
        title = unescape(re.sub(r"<[^>]+>", "", link.group(2))).strip()
        snippet = ""
        snip_m = snippet_re.search(block)
        if snip_m:
            snippet = unescape(re.sub(r"<[^>]+>", "", snip_m.group(1))).strip()
        if title and href:
            results.append({"title": title, "url": href, "snippet": snippet})
    return results


def _is_safe_http_url(url: str) -> bool:
    """Aceita apenas http(s) absolutos. Bloqueia javascript:, data:, file:, etc."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _clean_ddg_url(href: str) -> str:
    """Decodifica os links de redirect do DuckDuckGo (uddg=...).

    Sempre retorna URL http(s) ou string vazia (descarta o resultado).
    """
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urlparse(href)
    except Exception:
        return ""
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            decoded = unquote(qs["uddg"][0])
            return decoded if _is_safe_http_url(decoded) else ""
    return href if _is_safe_http_url(href) else ""


def _format_results(results: list[dict[str, str]]) -> str:
    """Formata os resultados como markdown numerado."""
    if not results:
        return "(sem resultados)"
    parts: list[str] = []
    for i, r in enumerate(results, start=1):
        parts.append(
            f"{i}. **{r.get('title', '')}** — {r.get('url', '')}\n"
            f"   {r.get('snippet', '')}\n"
        )
    return "\n".join(parts)
