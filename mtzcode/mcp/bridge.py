"""Bridge entre tools MCP e o sistema de tools nativo do mtzcode.

Cada tool exposta por um servidor MCP é embrulhada em uma instância de
`MCPToolBridge`, que implementa a interface `Tool` (Args pydantic + run).
A função `register_mcp_tools` registra todas as tools no `ToolRegistry`.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ..tools.base import Tool, ToolError, ToolRegistry
from .client import MCPManager

logger = logging.getLogger(__name__)


class _MCPArgs(BaseModel):
    """Args genéricos para tools MCP — repassa um dict opaco ao servidor.

    O modelo usa um único campo `arguments` (dict) porque o inputSchema
    de cada tool MCP é arbitrário; deixamos o servidor validar.
    """

    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Dicionário de argumentos conforme o inputSchema da tool MCP.",
    )


class _LoopRunner:
    """Mantém um event loop dedicado em uma thread de fundo.

    Usado para chamar funções async do MCP a partir de `run()` síncrono
    sem conflitar com loops já em execução (ex.: dentro do FastAPI).
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro: Any) -> Any:
        """Executa uma coroutine no loop de fundo e bloqueia até o resultado."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result()


# Loop compartilhado entre todas as instâncias de MCPToolBridge.
_shared_runner: _LoopRunner | None = None


def _get_runner() -> _LoopRunner:
    global _shared_runner
    if _shared_runner is None:
        _shared_runner = _LoopRunner()
    return _shared_runner


class MCPToolBridge(Tool):
    """Adapta uma tool MCP para a interface `Tool` do mtzcode."""

    # Tools MCP são tratadas como destrutivas por padrão (efeitos desconhecidos).
    destructive: ClassVar[bool] = True
    Args = _MCPArgs

    def __init__(
        self,
        manager: MCPManager,
        server_name: str,
        mcp_tool_info: dict[str, Any],
    ) -> None:
        self._manager = manager
        self._server_name = server_name
        self._mcp_name = mcp_tool_info.get("name", "unknown")
        # Nome final exposto ao modelo: prefixado para evitar colisão.
        self.name = f"mcp_{server_name}_{self._mcp_name}"  # type: ignore[misc]
        desc = mcp_tool_info.get("description", "") or ""
        self.description = (  # type: ignore[misc]
            f"[MCP:{server_name}] {desc}".strip()
            or f"Tool MCP `{self._mcp_name}` do servidor `{server_name}`."
        )
        # Guardamos o inputSchema original caso alguém queira inspecionar.
        self._input_schema = mcp_tool_info.get("inputSchema", {}) or {}

    def schema(self) -> dict[str, Any]:
        """Schema OpenAI/Ollama-style usando o inputSchema original do MCP."""
        # Se o servidor declarou um schema, usamos ele direto — assim o modelo
        # vê os parâmetros reais ao invés de um dict opaco.
        params = self._input_schema or _MCPArgs.model_json_schema()
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params,
            },
        }

    def call(self, raw_args: dict[str, Any]) -> str:
        """Override: aceita o dict cru sem validar pelo _MCPArgs.

        Como o schema declarado é o do próprio MCP, o modelo manda os
        argumentos no nível raiz — não dentro de `arguments`.
        """
        try:
            result = self._invoke(raw_args or {})
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"erro em `{self.name}`: {exc}") from exc
        if not isinstance(result, str):
            result = str(result)
        return result

    def run(self, args: BaseModel) -> str:  # pragma: no cover - usado só se chamarem direto
        """Caminho alternativo: usa o campo `arguments` do _MCPArgs."""
        payload: dict[str, Any] = {}
        if isinstance(args, _MCPArgs):
            payload = args.arguments
        return self._invoke(payload)

    def _invoke(self, payload: dict[str, Any]) -> str:
        """Chama a tool MCP de forma síncrona usando o loop de fundo."""
        runner = _get_runner()
        return runner.submit(
            self._manager.call_tool(self._server_name, self._mcp_name, payload)
        )


def register_mcp_tools(registry: ToolRegistry, manager: MCPManager) -> int:
    """Lista as tools de todos os servidores MCP e registra no `ToolRegistry`.

    Retorna o número de tools registradas. Pressupõe que `manager.connect_all()`
    já foi chamado. Usa um event loop temporário se necessário.
    """
    runner = _get_runner()
    try:
        tools_info = runner.submit(manager.list_all_tools())
    except Exception as exc:  # noqa: BLE001
        logger.error("falha ao listar tools MCP: %s", exc)
        return 0

    count = 0
    for info in tools_info:
        server = info.get("server", "")
        if not server or not info.get("name"):
            continue
        bridge = MCPToolBridge(manager, server, info)
        try:
            registry.register(bridge)
            count += 1
        except ValueError as exc:
            # Tool duplicada — só logamos e seguimos.
            logger.warning("não registrou tool MCP `%s`: %s", bridge.name, exc)
    logger.info("registradas %d tools MCP", count)
    return count
