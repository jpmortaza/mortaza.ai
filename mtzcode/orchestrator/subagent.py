"""Sub-agentes — fase 2 do orquestrador (Executor).

A ideia é dar ao agente principal o poder de **delegar uma tarefa específica**
pra um sub-agente isolado: contexto novo, system prompt focado, subset
limitado de tools. O sub-agente executa, retorna a resposta final como string,
e some — não polui o histórico do principal com lixo intermediário.

Casos de uso:
  - "pesquise X na web e me devolva 3 bullets" → sub-agente com só web_search/web_fetch
  - "implemente o backend dessa tarefa do plano" → sub-agente com filesystem + bash
  - "escreva os testes pra esse arquivo" → sub-agente com read/write/bash
  - exploração isolada que se for feita no contexto principal explode tokens

A profundidade é limitada por ``runtime.MAX_SUBAGENT_DEPTH`` pra impedir
recursão infinita (sub-agente chamando sub-agente chamando...).
"""
from __future__ import annotations

from typing import Any

from mtzcode import runtime
from mtzcode.tools.base import Tool, ToolError, ToolRegistry

# Tools que NUNCA são herdadas pelo sub-agente: planning e spawning são
# privilégio do agente principal. Sub-agentes executam, não orquestram.
_BLOCKED_TOOLS = {
    "spawn_agent",
    "plan_task",
    "plan_show",
    "plan_set_status",
    "plan_advance",
    "plan_list",
    "todo_write",
    "todo_read",
}


SUBAGENT_SYSTEM_TEMPLATE = """Você é um SUB-AGENTE focado, criado por um agente principal pra executar UMA tarefa específica e retornar o resultado.

Papel: {role}

Tarefa que você deve executar:
{task}

Regras:
- Foco TOTAL nessa tarefa. Não desvie, não pergunte, não converse.
- Use as tools disponíveis pra executar.
- Quando terminar, devolva uma resposta final CURTA e DIRETA — só o que o agente principal precisa saber pra continuar. Sem preâmbulo, sem "ok feito", sem listar o que você fez.
- Se a tarefa for impossível, devolva "FALHOU: <motivo curto>".
- NÃO crie outras tarefas, NÃO planeje, NÃO delegue. Você é a folha da árvore.
"""


def _filter_registry(
    parent: ToolRegistry, allowed: list[str] | None
) -> ToolRegistry:
    """Cria um novo ToolRegistry com um subset das tools do pai.

    Sempre exclui as tools de `_BLOCKED_TOOLS`. Se ``allowed`` for None,
    pega tudo do pai (menos as bloqueadas).
    """
    tools: list[Tool] = []
    parent_names = set(parent.names())
    if allowed:
        wanted = [n.strip() for n in allowed if n and n.strip()]
        unknown = [n for n in wanted if n not in parent_names]
        if unknown:
            raise ToolError(
                f"tools desconhecidas pro sub-agente: {', '.join(unknown)}. "
                f"Disponíveis: {', '.join(sorted(parent_names))}"
            )
        for name in wanted:
            if name in _BLOCKED_TOOLS:
                continue
            tools.append(parent.get(name))
    else:
        for name in parent.names():
            if name in _BLOCKED_TOOLS:
                continue
            tools.append(parent.get(name))
    if not tools:
        raise ToolError("sub-agente precisa de pelo menos 1 tool habilitada")
    return ToolRegistry(tools)


def run_subagent(
    *,
    task: str,
    role: str,
    tools: list[str] | None = None,
    max_iterations: int = 12,
    label: str = "sub",
) -> dict[str, Any]:
    """Executa um sub-agente sincronamente. Retorna ``{result, iterations, tool_calls}``.

    - Reusa cliente E registry do agente ativo (via ``runtime.current()``).
    - Limita profundidade via ``runtime.MAX_SUBAGENT_DEPTH``.
    - Filtra tools (subset opcional + bloqueio de planning/spawn).
    """
    parent_ctx = runtime.current()
    if parent_ctx is None:
        raise ToolError("sem agente ativo — spawn_agent só funciona dentro do loop")
    if parent_ctx.registry is None:
        raise ToolError("agente ativo não expôs registry — não dá pra criar sub-agente")
    if parent_ctx.depth + 1 >= runtime.MAX_SUBAGENT_DEPTH:
        raise ToolError(
            f"profundidade máxima de sub-agentes atingida "
            f"({runtime.MAX_SUBAGENT_DEPTH}). Sub-agente não pode delegar mais."
        )

    # Import lazy pra evitar ciclo: tools/__init__ -> tools/orchestrator
    # -> subagent -> agent -> tools/base -> tools/__init__ (loop).
    from mtzcode.agent import Agent

    sub_registry = _filter_registry(parent_ctx.registry, tools)

    system_prompt = SUBAGENT_SYSTEM_TEMPLATE.format(
        role=role.strip() or "executor",
        task=task.strip(),
    )

    # Histórico do sub-agente é isolado: começa do zero, só system prompt.
    sub_agent = Agent(
        client=parent_ctx.client,
        registry=sub_registry,
        system_prompt=system_prompt,
        max_iterations=max_iterations,
        runtime_label=label,
        runtime_depth=parent_ctx.depth + 1,
    )

    # Mensagem do "user" pro sub-agente é simplesmente o sinal pra começar.
    # A tarefa real já tá no system prompt.
    kickoff = "Execute a tarefa acima agora e retorne só o resultado final."

    tool_call_count = 0
    iterations = [0]

    def _on_event(ev) -> None:
        nonlocal tool_call_count
        if ev.kind == "tool_call":
            tool_call_count += 1
        # Conta iterações como número de assistant_text/end (proxy)
        if ev.kind in ("assistant_text", "assistant_text_end"):
            iterations[0] += 1

    try:
        result = sub_agent.run(kickoff, on_event=_on_event)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"sub-agente falhou: {exc}") from exc

    return {
        "result": (result or "").strip(),
        "tool_calls": tool_call_count,
        "iterations": iterations[0],
        "depth": parent_ctx.depth + 1,
    }
