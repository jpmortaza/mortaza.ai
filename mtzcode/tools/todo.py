"""TodoWriteTool — permite ao modelo manter uma lista de tarefas persistente.

O modelo chama essa tool passando a lista INTEIRA a cada atualização. A
lista vira o "painel de controle" visual da UI (aba Tarefas) e sobrevive
entre mensagens e sessões.

Fluxo típico:
  1. Usuário pede algo grande: "cria um SaaS de agenda com login e pagamento"
  2. Modelo chama todo_write com uma lista de tarefas (status=pending)
  3. A cada tarefa que começa, chama de novo marcando aquela como in_progress
  4. Ao terminar, marca completed e passa pra próxima
  5. UI espelha tudo em tempo real
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from mtzcode import todos as _todos
from mtzcode.tools.base import Tool, ToolError


class TodoItem(BaseModel):
    content: str = Field(..., description="Descrição curta da tarefa.")
    status: str = Field(
        "pending",
        description="Status: 'pending', 'in_progress' ou 'completed'.",
    )
    id: str | None = Field(
        None,
        description="ID opcional (se vazio, gerado como t1, t2, ...).",
    )


class TodoWriteArgs(BaseModel):
    todos: list[TodoItem] = Field(
        ...,
        description=(
            "Lista COMPLETA de tarefas — sobrescreve a anterior. "
            "Mantenha exatamente UMA tarefa como 'in_progress' por vez."
        ),
    )


class TodoWriteTool(Tool):
    name = "todo_write"
    destructive = False
    description = (
        "Cria/atualiza a lista de tarefas persistente (vira painel visual na UI). "
        "Use sempre que o pedido do usuário tiver 3+ passos, ou sempre que estiver "
        "orquestrando algo longo. Passe a lista INTEIRA a cada call — é sobrescrita. "
        "Mantenha 1 tarefa 'in_progress' por vez e marque 'completed' assim que "
        "cada uma terminar (não acumule). Status válidos: pending, in_progress, completed."
    )
    Args = TodoWriteArgs

    def run(self, args: TodoWriteArgs) -> str:  # type: ignore[override]
        try:
            raw = [t.model_dump() for t in args.todos]
            saved = _todos.save_todos(raw)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        summary = saved.get("summary") or {}
        total = summary.get("total", 0)
        done = summary.get("completed", 0)
        prog = summary.get("in_progress", 0)
        pend = summary.get("pending", 0)
        lines = [
            f"lista atualizada: {total} tarefas "
            f"({done} done, {prog} em andamento, {pend} pendente)"
        ]
        for i, t in enumerate(saved.get("todos") or [], start=1):
            icon = {
                "completed": "✓",
                "in_progress": "▶",
                "pending": "○",
            }.get(t.get("status"), "?")
            lines.append(f"  {icon} {i}. {t.get('content')}")
        return "\n".join(lines)


class TodoReadArgs(BaseModel):
    pass


class TodoReadTool(Tool):
    name = "todo_read"
    destructive = False
    description = (
        "Lê a lista de tarefas atual. Use se perder o estado ou no início "
        "de uma sessão nova pra saber se tem trabalho pendente."
    )
    Args = TodoReadArgs

    def run(self, args: TodoReadArgs) -> str:  # type: ignore[override]
        data = _todos.load_todos()
        items = data.get("todos") or []
        if not items:
            return "(sem tarefas na lista — use todo_write pra criar uma)"
        summary = _todos.summarize(items)
        lines = [
            f"{summary['total']} tarefas "
            f"({summary['completed']} done, "
            f"{summary['in_progress']} em andamento, "
            f"{summary['pending']} pendente)"
        ]
        for i, t in enumerate(items, start=1):
            icon = {
                "completed": "✓",
                "in_progress": "▶",
                "pending": "○",
            }.get(t.get("status"), "?")
            lines.append(f"  {icon} {i}. {t.get('content')}")
        return "\n".join(lines)
