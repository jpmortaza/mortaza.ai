"""BrowserTool — controla um navegador real via Playwright (Chromium).

Mantém uma instância singleton de Playwright + browser + page entre chamadas
da tool, pra preservar estado (cookies, sessão, página atual) ao longo de
múltiplas ações do modelo. Usa lazy init na primeira chamada e registra um
handler de atexit pra fechar tudo limpinho ao sair do processo.

Imports do `playwright` são feitos lazy dentro das funções pra que o módulo
possa ser importado mesmo sem o pacote instalado — só vai falhar com mensagem
clara quando o usuário tentar de fato usar a tool.
"""
from __future__ import annotations

import atexit
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError

# Limite de chars retornados em ações que devolvem texto da página.
MAX_TEXT_OUTPUT = 4000


# ---------------------------------------------------------------------------
# Estado singleton do navegador
# ---------------------------------------------------------------------------
# Mantemos referências em nível de módulo pra reaproveitar a mesma sessão
# entre chamadas sucessivas da tool. Sem isso, cada `browser(...)` abriria
# um Chromium novo e perderia login/cookies/etc.
_browser_state: dict[str, Any] = {
    "playwright": None,  # instância retornada por sync_playwright().start()
    "browser": None,     # instância de Browser
    "context": None,     # BrowserContext (1 por sessão)
    "page": None,        # Page atual
    "headless": False,   # modo definido no primeiro init
}


def _get_page(headless: bool = False) -> Any:
    """Retorna a página atual, inicializando o navegador se necessário.

    O parâmetro `headless` só tem efeito na primeira chamada (quando o
    browser ainda não foi iniciado). Chamadas posteriores reaproveitam a
    instância existente independentemente do valor passado.
    """
    if _browser_state["page"] is not None:
        return _browser_state["page"]

    # Import lazy: se Playwright não tiver instalado, ImportError sobe daqui
    # e é capturado no `run()` da tool com mensagem amigável.
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=headless)
    context = browser.new_context()
    page = context.new_page()

    _browser_state["playwright"] = pw
    _browser_state["browser"] = browser
    _browser_state["context"] = context
    _browser_state["page"] = page
    _browser_state["headless"] = headless

    return page


def _shutdown_browser() -> None:
    """Fecha browser + playwright. Idempotente. Registrado em atexit."""
    page = _browser_state.get("page")
    context = _browser_state.get("context")
    browser = _browser_state.get("browser")
    pw = _browser_state.get("playwright")

    # Fecha em ordem reversa, ignorando qualquer erro (já estamos saindo).
    for obj in (page, context, browser):
        if obj is not None:
            try:
                obj.close()
            except Exception:  # noqa: BLE001 — best effort no shutdown
                pass
    if pw is not None:
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass

    _browser_state["page"] = None
    _browser_state["context"] = None
    _browser_state["browser"] = None
    _browser_state["playwright"] = None


# Garante que o Chromium não fique zumbi quando o processo do mtzcode termina.
atexit.register(_shutdown_browser)


# ---------------------------------------------------------------------------
# Args + Tool
# ---------------------------------------------------------------------------


class BrowserArgs(BaseModel):
    action: Literal[
        "navigate",
        "click",
        "type",
        "screenshot",
        "eval",
        "text",
        "wait",
        "back",
        "forward",
    ] = Field(..., description="Ação a executar no navegador.")
    url: str | None = Field(
        None, description="URL para a ação `navigate`."
    )
    selector: str | None = Field(
        None,
        description="Seletor CSS para `click`, `type` e `wait`.",
    )
    text: str | None = Field(
        None, description="Texto a digitar quando action=`type`."
    )
    script: str | None = Field(
        None, description="Código JavaScript a executar quando action=`eval`."
    )
    timeout_ms: int = Field(
        10_000,
        description="Timeout em milissegundos para esperas/navegação (default 10s).",
        ge=100,
        le=120_000,
    )
    headless: bool = Field(
        False,
        description=(
            "Se True, abre Chromium headless. Só tem efeito na primeira "
            "chamada (quando o browser ainda não foi iniciado)."
        ),
    )


class BrowserTool(Tool):
    name = "browser"
    destructive = True  # pode submeter forms, clicar em botões reais, etc.
    description = (
        "Controla um navegador real (Chromium via Playwright). "
        "Permite navegar, clicar, digitar, tirar screenshot, rodar JS. "
        "Use para tarefas web interativas (login, preencher forms, scraping complexo). "
        "Para leitura simples de páginas use web_fetch que é mais rápido."
    )
    Args = BrowserArgs

    def run(self, args: BrowserArgs) -> str:  # type: ignore[override]
        # Captura ImportError aqui pra dar mensagem amigável se o usuário
        # ainda não instalou o Playwright nem baixou o Chromium.
        try:
            page = _get_page(headless=args.headless)
        except ImportError:
            raise ToolError(
                "Playwright não está instalado. Rode: "
                "`pip install playwright && playwright install chromium`"
            ) from None
        except Exception as exc:  # noqa: BLE001
            # Erro típico: Chromium não baixado ainda.
            raise ToolError(
                f"falha ao iniciar navegador: {exc}. "
                "Talvez falte rodar `playwright install chromium`."
            ) from exc

        action = args.action
        try:
            if action == "navigate":
                return _do_navigate(page, args)
            if action == "click":
                return _do_click(page, args)
            if action == "type":
                return _do_type(page, args)
            if action == "screenshot":
                return _do_screenshot(page)
            if action == "eval":
                return _do_eval(page, args)
            if action == "text":
                return _do_text(page)
            if action == "wait":
                return _do_wait(page, args)
            if action == "back":
                page.go_back(timeout=args.timeout_ms)
                return f"voltou para: {page.url}"
            if action == "forward":
                page.go_forward(timeout=args.timeout_ms)
                return f"avançou para: {page.url}"
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"falha em browser.{action}: {exc}") from exc

        # Não deveria chegar aqui (Literal cobre todos os casos).
        raise ToolError(f"ação desconhecida: {action}")


# ---------------------------------------------------------------------------
# Helpers internos por ação
# ---------------------------------------------------------------------------


def _require(value: Any, field: str, action: str) -> Any:
    """Valida que um campo obrigatório foi passado pra ação X."""
    if value is None or value == "":
        raise ToolError(f"`{field}` é obrigatório para action=`{action}`")
    return value


def _do_navigate(page: Any, args: BrowserArgs) -> str:
    url = _require(args.url, "url", "navigate")
    page.goto(url, timeout=args.timeout_ms)
    title = page.title() or "(sem título)"
    return f"navegou para {url}, título: {title}"


def _do_click(page: Any, args: BrowserArgs) -> str:
    selector = _require(args.selector, "selector", "click")
    page.click(selector, timeout=args.timeout_ms)
    return f"clicou em {selector}"


def _do_type(page: Any, args: BrowserArgs) -> str:
    selector = _require(args.selector, "selector", "type")
    text = _require(args.text, "text", "type")
    # `fill` substitui o conteúdo — comportamento mais previsível que `type`
    # pra inputs de form. Pra simular digitação tecla a tecla use eval.
    page.fill(selector, text, timeout=args.timeout_ms)
    return f"digitou em {selector}"


def _do_screenshot(page: Any) -> str:
    ts = int(time.time() * 1000)
    path = f"/tmp/mtzcode-screenshot-{ts}.png"
    page.screenshot(path=path, full_page=True)
    return path


def _do_eval(page: Any, args: BrowserArgs) -> str:
    script = _require(args.script, "script", "eval")
    result = page.evaluate(script)
    return str(result)


def _do_text(page: Any) -> str:
    body_text = page.inner_text("body")
    if len(body_text) > MAX_TEXT_OUTPUT:
        return body_text[:MAX_TEXT_OUTPUT] + f"\n... (texto truncado em {MAX_TEXT_OUTPUT} chars)"
    return body_text


def _do_wait(page: Any, args: BrowserArgs) -> str:
    selector = _require(args.selector, "selector", "wait")
    page.wait_for_selector(selector, timeout=args.timeout_ms)
    return f"selector {selector} apareceu"
