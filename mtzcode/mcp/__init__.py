"""Suporte a MCP (Model Context Protocol) no mtzcode.

Este pacote permite conectar o mtzcode a servidores MCP externos
(Gmail, Notion, GitHub, filesystem, etc) usando o SDK oficial `mcp`.
"""
from __future__ import annotations

from .client import MCPManager, MCPServerConfig

__all__ = ["MCPManager", "MCPServerConfig"]
