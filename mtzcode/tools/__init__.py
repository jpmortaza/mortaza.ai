"""Registry de tools do mtzcode.

Por padrão carrega só as tools CORE (filesystem + bash + web básico) pra
manter o contexto enxuto e a inferência rápida em modelos locais.

Tool groups extras (macos, browser, documents) são opt-in via env var
`MTZCODE_TOOL_GROUPS` (CSV) ou via `default_registry(groups=[...])`.

Exemplo:
    MTZCODE_TOOL_GROUPS=core,macos,documents mtzcode
    MTZCODE_TOOL_GROUPS=all mtzcode serve
"""
from __future__ import annotations

import os

from mtzcode.tools.apify import (
    ApifyGetDatasetTool,
    ApifyListActorsTool,
    ApifyRunActorTool,
)
from mtzcode.tools.applescript import AppleScriptTool
from mtzcode.tools.bash import BashTool
from mtzcode.tools.base import Tool, ToolError, ToolRegistry
from mtzcode.tools.browser import BrowserTool
from mtzcode.tools.clipboard import ClipboardReadTool, ClipboardWriteTool
from mtzcode.tools.docx import DocxReadTool, DocxWriteTool
from mtzcode.tools.edit import EditTool
from mtzcode.tools.find_files import FindFilesTool
from mtzcode.tools.find_images import FindImagesTool
from mtzcode.tools.github import (
    GhAnalyzeRepoTool,
    GhCloneTool,
    GhListReposTool,
    GhPushFolderTool,
    GhRepoInfoTool,
)
from mtzcode.tools.glob import GlobTool
from mtzcode.tools.grep import GrepTool
from mtzcode.tools.notify import NotifyTool
from mtzcode.tools.open_app import OpenAppTool
from mtzcode.tools.open_url import OpenUrlTool
from mtzcode.tools.orchestrator import (
    PlanAdvanceTool,
    PlanListTool,
    PlanSetStatusTool,
    PlanShowTool,
    PlanTaskTool,
    SpawnAgentTool,
)
from mtzcode.tools.pdf import PdfFromMarkdownTool, PdfReadTool
from mtzcode.tools.python_exec import PythonExecTool
from mtzcode.tools.read import ReadTool
from mtzcode.tools.screenshot import ScreenshotTool
from mtzcode.tools.search import SearchCodeTool
from mtzcode.tools.search_knowledge import SearchKnowledgeTool
from mtzcode.tools.text_writer import TextWriterTool
from mtzcode.tools.todo import TodoReadTool, TodoWriteTool
from mtzcode.tools.web_fetch import WebFetchTool
from mtzcode.tools.web_search import WebSearchTool
from mtzcode.tools.write import WriteTool
from mtzcode.tools.xlsx import XlsxReadTool, XlsxWriteTool


# Grupos de tools — controlam o tamanho do contexto enviado ao modelo.
TOOL_GROUPS: dict[str, list[type[Tool]]] = {
    # CORE: estritamente necessário pra trabalhar com código (10 tools)
    "core": [
        ReadTool,
        WriteTool,
        EditTool,
        GlobTool,
        GrepTool,
        SearchCodeTool,
        SearchKnowledgeTool,
        BashTool,
        PythonExecTool,
        TodoWriteTool,
        TodoReadTool,
        PlanTaskTool,
        PlanShowTool,
        PlanSetStatusTool,
        PlanAdvanceTool,
        PlanListTool,
        SpawnAgentTool,
        WebFetchTool,
        OpenUrlTool,
    ],
    # WEB: pesquisa e navegador real
    "web": [WebSearchTool, BrowserTool],
    # APIFY: scraping/automação via plataforma Apify (precisa APIFY_API_KEY)
    "apify": [ApifyRunActorTool, ApifyListActorsTool, ApifyGetDatasetTool],
    # GITHUB: clone/info/push de repos via gh CLI (precisa `gh auth login`)
    "github": [
        GhCloneTool,
        GhRepoInfoTool,
        GhListReposTool,
        GhPushFolderTool,
        GhAnalyzeRepoTool,
    ],
    # MACOS: controle do sistema (apps, clipboard, screenshot)
    # NOTA: NotifyTool foi tirado do default — modelos locais confundiam
    # `notify` com "responder ao usuário" e disparavam popups em vez de
    # texto. Pra reativar use o grupo "macos-notify" explicitamente.
    "macos": [
        AppleScriptTool,
        ClipboardReadTool,
        ClipboardWriteTool,
        ScreenshotTool,
        OpenAppTool,
    ],
    "macos-notify": [NotifyTool],
    # SUPERPOWERS: busca em qualquer lugar do disco via Spotlight
    "superpowers": [
        FindFilesTool,
        FindImagesTool,
    ],
    # DOCUMENTS: leitura/escrita de docx, pdf, xlsx, textos longos
    "documents": [
        DocxReadTool,
        DocxWriteTool,
        PdfReadTool,
        PdfFromMarkdownTool,
        XlsxReadTool,
        XlsxWriteTool,
        TextWriterTool,
    ],
}


def _resolve_groups(groups: list[str] | None) -> list[str]:
    """Resolve a lista de grupos a partir de arg explícito, env var ou default."""
    if groups:
        return groups
    env = os.environ.get("MTZCODE_TOOL_GROUPS", "").strip()
    if env:
        return [g.strip() for g in env.split(",") if g.strip()]
    return ["core"]


def _build_inner_registry(groups: list[str] | None = None) -> ToolRegistry:
    """Constrói um ToolRegistry "cru" com as tools dos grupos pedidos.

    Por padrão (sem args/env) retorna TODAS as habilidades — o overhead
    de schema agora é absorvido pela `SkillRegistry`, que expõe só 2
    meta-tools ao modelo. Os grupos só servem pra desativar categorias
    inteiras quando o usuário quiser.
    """
    requested = _resolve_groups(groups)
    if "all" in requested:
        # "all" exclui grupos marcados como opt-in (NotifyTool atrapalha
        # demais o modelo local — só carrega se explicitamente listado).
        opt_in = {"macos-notify"}
        requested = [g for g in TOOL_GROUPS.keys() if g not in opt_in]

    seen: set[type[Tool]] = set()
    tools: list[Tool] = []
    for group in requested:
        if group not in TOOL_GROUPS:
            continue
        for cls in TOOL_GROUPS[group]:
            if cls in seen:
                continue
            seen.add(cls)
            tools.append(cls())
    return ToolRegistry(tools)


def default_registry(groups: list[str] | None = None):
    """Registry padrão do mtzcode — todas as habilidades expostas direto.

    A indireção meta-tool (SkillRegistry) confundia modelos locais Q4
    porque exigia JSON aninhado (`usar_habilidade(nome=X, argumentos={...})`).
    Voltamos a expor tudo direto, mas com schemas slim (descrições curtas
    + remoção de ruído do JSONSchema do Pydantic) — reduz ~60% do
    overhead de contexto sem perder funcionalidade.
    """
    # Por padrão habilita TUDO (env var MTZCODE_TOOL_GROUPS pode restringir)
    if groups is None and not os.environ.get("MTZCODE_TOOL_GROUPS"):
        groups = ["all"]
    return _build_inner_registry(groups)


__all__ = [
    "Tool",
    "ToolError",
    "ToolRegistry",
    "TOOL_GROUPS",
    "default_registry",
    "ReadTool",
    "WriteTool",
    "EditTool",
    "BashTool",
    "PythonExecTool",
    "TodoWriteTool",
    "TodoReadTool",
    "PlanTaskTool",
    "PlanShowTool",
    "PlanSetStatusTool",
    "PlanAdvanceTool",
    "PlanListTool",
    "SpawnAgentTool",
    "FindFilesTool",
    "FindImagesTool",
    "GlobTool",
    "GrepTool",
    "SearchCodeTool",
    "SearchKnowledgeTool",
    "WebFetchTool",
    "WebSearchTool",
    "OpenUrlTool",
    "BrowserTool",
    "AppleScriptTool",
    "ClipboardReadTool",
    "ClipboardWriteTool",
    "NotifyTool",
    "ScreenshotTool",
    "OpenAppTool",
    "DocxReadTool",
    "DocxWriteTool",
    "PdfReadTool",
    "PdfFromMarkdownTool",
    "XlsxReadTool",
    "XlsxWriteTool",
    "TextWriterTool",
    "ApifyRunActorTool",
    "ApifyListActorsTool",
    "ApifyGetDatasetTool",
    "GhCloneTool",
    "GhRepoInfoTool",
    "GhListReposTool",
    "GhPushFolderTool",
    "GhAnalyzeRepoTool",
]
