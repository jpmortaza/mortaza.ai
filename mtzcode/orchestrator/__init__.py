"""Orquestrador do mtzcode — fase 1: Planner.

A ideia: pedidos grandes ("crie um SaaS de agendamento com login e pagamento")
passam por um planner que gera um plano estruturado em fases → tarefas, persiste
em disco e espelha na TODO list pra visualização em tempo real.

Fases futuras:
  - Phase 2: Executor — sub-agentes que executam tarefas isoladas
  - Phase 3: Verifier — agente que valida cada fase antes de avançar

Por enquanto só Phase 1: o modelo principal monta o plano (via tool ``plan_task``),
ele fica visível pro usuário, e as próximas mensagens podem dar ``plan_advance``
pra avançar a tarefa atual.
"""
from mtzcode.orchestrator.store import (
    advance_current,
    create_plan,
    current_plan,
    list_plans,
    load_plan,
    mirror_to_todos,
    set_task_status,
)

__all__ = [
    "advance_current",
    "create_plan",
    "current_plan",
    "list_plans",
    "load_plan",
    "mirror_to_todos",
    "set_task_status",
]
