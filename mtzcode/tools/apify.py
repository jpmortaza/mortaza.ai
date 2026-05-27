"""Tools de integração com Apify (https://apify.com).

Apify é uma plataforma de web scraping/automação. Tem milhares de "actors"
prontos (Google Maps scraper, Instagram scraper, Amazon, TikTok, etc) que a
gente dispara via API e recebe os dados estruturados.

Tools expostas:
  - apify_run_actor: roda um actor sincronamente e devolve o dataset
  - apify_list_actors: lista os actors do usuário (ou busca na store pública)
  - apify_get_dataset: lê itens de um dataset existente

Auth: variável de ambiente ``APIFY_API_KEY`` (ou ``APIFY_TOKEN``).
Configurável pelo painel de Configurações da web UI.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError

APIFY_BASE = "https://api.apify.com/v2"
DEFAULT_TIMEOUT = 120.0  # actors podem demorar
MAX_RESULT_CHARS = 12000


def _api_token() -> str:
    """Lê token de APIFY_API_KEY ou APIFY_TOKEN."""
    token = os.environ.get("APIFY_API_KEY") or os.environ.get("APIFY_TOKEN")
    if not token:
        raise ToolError(
            "APIFY_API_KEY não definida. Configure em Configurações > API Keys "
            "ou exporte a variável de ambiente. Pegue sua key em "
            "https://console.apify.com/account/integrations"
        )
    return token


def _httpx():
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise ToolError(
            "dependência ausente: instale `httpx` para usar tools do Apify."
        ) from exc
    return httpx


def _auth_headers(token: str) -> dict[str, str]:
    """Header padrão Apify (Bearer) — evita vazar token em URL/params em logs."""
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _safe_error_body(text: str, token: str) -> str:
    """Mascara o token em qualquer eco da request antes de devolver pro modelo."""
    snippet = (text or "")[:400]
    if token:
        snippet = snippet.replace(token, "***")
    return snippet


def _truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncado em {limit} chars)"


# ----------------------------------------------------------------------
# apify_run_actor
# ----------------------------------------------------------------------
class ApifyRunActorArgs(BaseModel):
    actor_id: str = Field(
        ...,
        description=(
            "ID ou slug do actor (ex: 'apify/google-maps-scraper' ou "
            "'compass~crawler-google-places'). Use ~ no lugar de / no slug."
        ),
    )
    input: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Input JSON do actor. Cada actor tem seu schema — veja a "
            "documentação do actor na Apify Store. Ex pra Google Maps: "
            '{"searchStringsArray": ["pizzaria SP"], "maxCrawledPlaces": 5}'
        ),
    )
    max_items: int = Field(
        20,
        description="Limite de itens do dataset a retornar (default 20).",
        ge=1,
        le=500,
    )
    timeout_s: int = Field(
        120,
        description="Timeout em segundos esperando o actor terminar.",
        ge=10,
        le=600,
    )


class ApifyRunActorTool(Tool):
    name = "apify_run_actor"
    destructive = False
    description = (
        "Roda um actor da Apify (sincronamente) e devolve os itens do dataset. "
        "Use pra scraping/automação: Google Maps, Instagram, Amazon, TikTok, "
        "Twitter, LinkedIn, etc. Procure o actor em https://apify.com/store. "
        "Cobra créditos da sua conta Apify."
    )
    Args = ApifyRunActorArgs

    def run(self, args: ApifyRunActorArgs) -> str:  # type: ignore[override]
        httpx = _httpx()
        token = _api_token()
        actor = args.actor_id.replace("/", "~")
        # Endpoint sync: roda o actor e já devolve os dataset items
        url = f"{APIFY_BASE}/acts/{actor}/run-sync-get-dataset-items"
        params = {"timeout": str(args.timeout_s), "limit": str(args.max_items)}
        try:
            r = httpx.post(
                url,
                params=params,
                json=args.input or {},
                timeout=DEFAULT_TIMEOUT,
                headers=_auth_headers(token),
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"falha de rede chamando Apify: {exc}") from exc
        if r.status_code >= 400:
            raise ToolError(
                f"Apify respondeu {r.status_code}: {_safe_error_body(r.text, token)}"
            )
        # O endpoint devolve uma JSON array dos items
        try:
            items = r.json()
        except json.JSONDecodeError:
            return _truncate(r.text)
        if not isinstance(items, list):
            return _truncate(json.dumps(items, ensure_ascii=False, indent=2))
        out_lines = [
            f"actor: {args.actor_id}",
            f"items retornados: {len(items)}",
            "--- dataset ---",
        ]
        for i, item in enumerate(items):
            out_lines.append(f"\n[{i}]")
            out_lines.append(json.dumps(item, ensure_ascii=False, indent=2))
        return _truncate("\n".join(out_lines))


# ----------------------------------------------------------------------
# apify_list_actors
# ----------------------------------------------------------------------
class ApifyListActorsArgs(BaseModel):
    search: Optional[str] = Field(
        None,
        description=(
            "Termo de busca na Apify Store pública (ex: 'instagram', "
            "'google maps'). Se vazio, lista os actors da SUA conta."
        ),
    )
    limit: int = Field(
        15,
        description="Quantos resultados retornar (default 15).",
        ge=1,
        le=50,
    )


class ApifyListActorsTool(Tool):
    name = "apify_list_actors"
    destructive = False
    description = (
        "Lista actors disponíveis no Apify. Sem `search` lista os SEUS actors; "
        "com `search` busca na store pública. Use antes de apify_run_actor "
        "pra descobrir o ID correto."
    )
    Args = ApifyListActorsArgs

    def run(self, args: ApifyListActorsArgs) -> str:  # type: ignore[override]
        httpx = _httpx()
        token = _api_token()
        if args.search:
            url = f"{APIFY_BASE}/store"
            params = {"search": args.search, "limit": str(args.limit)}
        else:
            url = f"{APIFY_BASE}/acts"
            params = {"limit": str(args.limit), "my": "true"}
        try:
            r = httpx.get(
                url,
                params=params,
                timeout=DEFAULT_TIMEOUT,
                headers=_auth_headers(token),
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"falha de rede chamando Apify: {exc}") from exc
        if r.status_code >= 400:
            raise ToolError(
                f"Apify respondeu {r.status_code}: {_safe_error_body(r.text, token)}"
            )
        try:
            data = r.json()
        except json.JSONDecodeError:
            return _truncate(r.text)
        items = (data.get("data") or {}).get("items") or []
        if not items:
            return "(nenhum actor encontrado)"
        lines = [f"encontrados: {len(items)}", "---"]
        for it in items:
            name = it.get("name") or it.get("title") or "?"
            username = it.get("username") or (it.get("user") or {}).get("username")
            slug = f"{username}/{name}" if username else name
            title = it.get("title") or ""
            desc = (it.get("description") or "")[:140]
            lines.append(f"\n• {slug}")
            if title and title != name:
                lines.append(f"  {title}")
            if desc:
                lines.append(f"  {desc}")
        return _truncate("\n".join(lines))


# ----------------------------------------------------------------------
# apify_get_dataset
# ----------------------------------------------------------------------
class ApifyGetDatasetArgs(BaseModel):
    dataset_id: str = Field(
        ...,
        description="ID do dataset (vem do retorno de uma run anterior).",
    )
    limit: int = Field(
        20,
        description="Quantos itens carregar (default 20).",
        ge=1,
        le=500,
    )
    offset: int = Field(0, description="Offset (paginação).", ge=0)


class ApifyGetDatasetTool(Tool):
    name = "apify_get_dataset"
    destructive = False
    description = (
        "Lê itens de um dataset Apify existente. Use quando você já tem um "
        "dataset_id (de uma run anterior) e quer ler/paginar os resultados."
    )
    Args = ApifyGetDatasetArgs

    def run(self, args: ApifyGetDatasetArgs) -> str:  # type: ignore[override]
        httpx = _httpx()
        token = _api_token()
        url = f"{APIFY_BASE}/datasets/{args.dataset_id}/items"
        params = {
            "limit": str(args.limit),
            "offset": str(args.offset),
            "format": "json",
            "clean": "true",
        }
        try:
            r = httpx.get(
                url,
                params=params,
                timeout=DEFAULT_TIMEOUT,
                headers=_auth_headers(token),
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"falha de rede chamando Apify: {exc}") from exc
        if r.status_code >= 400:
            raise ToolError(
                f"Apify respondeu {r.status_code}: {_safe_error_body(r.text, token)}"
            )
        try:
            items = r.json()
        except json.JSONDecodeError:
            return _truncate(r.text)
        if not isinstance(items, list):
            return _truncate(json.dumps(items, ensure_ascii=False, indent=2))
        out = [
            f"dataset: {args.dataset_id}",
            f"items: {len(items)} (offset={args.offset})",
            "---",
        ]
        for i, item in enumerate(items):
            out.append(f"\n[{args.offset + i}]")
            out.append(json.dumps(item, ensure_ascii=False, indent=2))
        return _truncate("\n".join(out))
