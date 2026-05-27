"""Runtime context global do mtzcode.

Carrega informações do agente "ativo" (cliente, profile, depth) pra que
ferramentas que precisam delas — como ``spawn_agent`` — consigam reusar
sem precisar receber via argumento.

Funciona como um stack: cada agente (principal ou sub-agente) faz push
do próprio contexto antes de executar tools, e pop ao terminar. Sub-agentes
herdam o cliente do pai mas avançam o ``depth`` em 1, o que permite limitar
recursão.

Uso típico (no Agent):

    from mtzcode import runtime
    with runtime.activate(client=self.client, profile=self.profile):
        self._execute_tool_calls(...)

E numa tool:

    from mtzcode import runtime
    ctx = runtime.current()
    if ctx is None:
        raise ToolError("sem agente ativo")
    parent_client = ctx.client
"""
from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from mtzcode.client import ChatClient
    from mtzcode.profiles import Profile
    from mtzcode.tools.base import ToolRegistry

MAX_SUBAGENT_DEPTH = 3


@dataclass
class AgentContext:
    client: "ChatClient"
    profile: "Profile | None" = None
    registry: "ToolRegistry | None" = None
    depth: int = 0
    label: str = "main"
    extra: dict[str, Any] | None = None


_local = threading.local()


def _stack() -> list[AgentContext]:
    if not hasattr(_local, "stack"):
        _local.stack = []
    return _local.stack


def current() -> AgentContext | None:
    """Contexto do agente que está executando agora (topo do stack)."""
    s = _stack()
    return s[-1] if s else None


def push(ctx: AgentContext) -> None:
    _stack().append(ctx)


def pop() -> AgentContext | None:
    s = _stack()
    return s.pop() if s else None


@contextlib.contextmanager
def activate(
    client: "ChatClient",
    profile: "Profile | None" = None,
    registry: "ToolRegistry | None" = None,
    *,
    label: str = "main",
    depth: int | None = None,
) -> Iterator[AgentContext]:
    """Context manager: empilha um AgentContext e desempilha no fim."""
    parent = current()
    resolved_depth = depth if depth is not None else (parent.depth + 1 if parent else 0)
    ctx = AgentContext(
        client=client,
        profile=profile,
        registry=registry,
        depth=resolved_depth,
        label=label,
    )
    push(ctx)
    try:
        yield ctx
    finally:
        pop()
