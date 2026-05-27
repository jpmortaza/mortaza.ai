"""Cliente MCP — gerencia conexões com servidores Model Context Protocol.

Carrega configuração de `~/.mtzcode/mcp_servers.json` (formato compatível
com Claude Desktop) e expõe métodos assíncronos para listar e chamar
tools dos servidores conectados.

Se o SDK oficial `mcp` não estiver instalado, o `MCPManager` opera em modo
stub: emite um aviso, mas todos os métodos retornam vazio sem quebrar.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Importação opcional do SDK MCP. Se não estiver instalado seguimos em
# modo stub para não quebrar a aplicação inteira.
try:
    from mcp import ClientSession, StdioServerParameters  # type: ignore
    from mcp.client.stdio import stdio_client  # type: ignore

    _MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - depende do ambiente
    ClientSession = None  # type: ignore
    StdioServerParameters = None  # type: ignore
    stdio_client = None  # type: ignore
    _MCP_AVAILABLE = False


# Caminho padrão do arquivo de configuração (compatível com Claude Desktop).
DEFAULT_CONFIG_PATH = Path.home() / ".mtzcode" / "mcp_servers.json"


@dataclass
class MCPServerConfig:
    """Configuração de um único servidor MCP."""

    name: str
    # Comando + args completos para transporte stdio (ex.: ["npx","-y","@modelcontextprotocol/server-github"]).
    command: list[str] = field(default_factory=list)
    # URL para transporte sse/http (ainda não implementado neste módulo).
    url: str | None = None
    # Variáveis de ambiente repassadas ao processo do servidor.
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


class MCPManager:
    """Gerencia múltiplos servidores MCP conectados via stdio."""

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self.servers: dict[str, MCPServerConfig] = {}
        # Sessões ativas por servidor (preenchidas em connect_all).
        self.sessions: dict[str, Any] = {}
        # Cache de tools listadas por servidor: {server_name: [tool_info_dict, ...]}.
        self._tools_cache: dict[str, list[dict[str, Any]]] = {}
        # Pilha de contexto async usada para manter os transports vivos.
        self._exit_stack: AsyncExitStack | None = None

        if not _MCP_AVAILABLE:
            logger.warning(
                "SDK `mcp` não instalado — MCPManager rodando em modo stub. "
                "Instale com `pip install mcp` para habilitar servidores MCP."
            )

    # ------------------------------------------------------------------ config
    def load_config(self) -> None:
        """Carrega o arquivo JSON de configuração no formato Claude Desktop.

        Formato esperado::

            {
              "mcpServers": {
                "github": {
                  "command": "npx",
                  "args": ["-y", "@modelcontextprotocol/server-github"],
                  "env": {"GITHUB_TOKEN": "..."}
                }
              }
            }
        """
        if not self.config_path.exists():
            logger.info("arquivo de config MCP não encontrado em %s", self.config_path)
            self.servers = {}
            return

        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error("falha ao parsear %s: %s", self.config_path, exc)
            self.servers = {}
            return

        servers_section = raw.get("mcpServers", {}) or {}
        loaded: dict[str, MCPServerConfig] = {}
        for name, spec in servers_section.items():
            if not isinstance(spec, dict):
                continue
            # Monta o command completo: [command] + args.
            cmd: list[str] = []
            if "command" in spec and spec["command"]:
                cmd.append(str(spec["command"]))
            for arg in spec.get("args", []) or []:
                cmd.append(str(arg))

            cfg = MCPServerConfig(
                name=name,
                command=cmd,
                url=spec.get("url"),
                env=dict(spec.get("env", {}) or {}),
                enabled=bool(spec.get("enabled", True)),
            )
            loaded[name] = cfg

        self.servers = loaded
        logger.info("carregados %d servidores MCP de %s", len(loaded), self.config_path)

    # --------------------------------------------------------------- conexões
    async def connect_all(self) -> None:
        """Conecta a todos os servidores habilitados via stdio.

        Em modo stub (SDK não instalado) é um no-op silencioso.
        """
        if not _MCP_AVAILABLE:
            return

        # Garante que existe uma pilha viva para manter os transports.
        if self._exit_stack is None:
            self._exit_stack = AsyncExitStack()
            await self._exit_stack.__aenter__()

        for name, cfg in self.servers.items():
            if not cfg.enabled:
                continue
            if not cfg.command:
                logger.warning("servidor MCP `%s` sem command — pulando", name)
                continue
            try:
                params = StdioServerParameters(
                    command=cfg.command[0],
                    args=cfg.command[1:],
                    # Mescla env atual com o env declarado pelo servidor.
                    env={**os.environ, **cfg.env} if cfg.env else None,
                )
                # Abre o transport stdio e a sessão dentro da pilha.
                read, write = await self._exit_stack.enter_async_context(
                    stdio_client(params)
                )
                session = await self._exit_stack.enter_async_context(
                    ClientSession(read, write)
                )
                await session.initialize()
                self.sessions[name] = session
                logger.info("MCP server `%s` conectado", name)
            except Exception as exc:  # noqa: BLE001 — log e segue com os outros
                logger.error("falha ao conectar MCP `%s`: %s", name, exc)

    async def list_all_tools(self) -> list[dict[str, Any]]:
        """Lista todas as tools de todos os servidores conectados.

        Retorna lista de dicts no formato::

            {"server": "github", "name": "create_issue", "description": "...", "inputSchema": {...}}
        """
        if not _MCP_AVAILABLE:
            return []

        result: list[dict[str, Any]] = []
        for server_name, session in self.sessions.items():
            try:
                listed = await session.list_tools()
            except Exception as exc:  # noqa: BLE001
                logger.error("falha ao listar tools do MCP `%s`: %s", server_name, exc)
                continue

            tools_info: list[dict[str, Any]] = []
            # O SDK retorna um objeto com .tools (lista de Tool com name/description/inputSchema).
            for tool in getattr(listed, "tools", []) or []:
                info = {
                    "server": server_name,
                    "name": getattr(tool, "name", ""),
                    "description": getattr(tool, "description", "") or "",
                    "inputSchema": getattr(tool, "inputSchema", {}) or {},
                }
                tools_info.append(info)
                result.append(info)
            self._tools_cache[server_name] = tools_info
        return result

    async def call_tool(
        self, server_name: str, tool_name: str, args: dict[str, Any]
    ) -> str:
        """Chama uma tool em um servidor MCP específico e retorna texto.

        Concatena qualquer conteúdo de texto retornado em uma única string.
        """
        if not _MCP_AVAILABLE:
            return "[mcp] SDK não instalado — chamada ignorada"

        session = self.sessions.get(server_name)
        if session is None:
            raise RuntimeError(f"servidor MCP `{server_name}` não conectado")

        result = await session.call_tool(tool_name, args or {})

        # O SDK devolve um CallToolResult com .content (lista de TextContent/ImageContent etc).
        chunks: list[str] = []
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            if text:
                chunks.append(text)
            else:
                chunks.append(str(item))
        if getattr(result, "isError", False):
            return "[mcp:erro] " + "\n".join(chunks)
        return "\n".join(chunks) if chunks else ""

    async def close_all(self) -> None:
        """Fecha todas as sessões e libera os transports."""
        if self._exit_stack is not None:
            try:
                await self._exit_stack.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                logger.error("erro ao fechar MCP exit stack: %s", exc)
            self._exit_stack = None
        self.sessions.clear()
