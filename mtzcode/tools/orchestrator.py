"""Tools do orquestrador (fase 1: Planner).

Expostas pro modelo:

  - ``plan_task``: cria um plano estruturado pra um pedido grande. Sobrescreve
    o plano corrente e espelha na TODO list pra UI mostrar.
  - ``plan_show``: mostra o plano corrente (ou outro pelo id) com fases e
    status de cada tarefa.
  - ``plan_set_status``: muda o status de uma task específica do plano corrente.
  - ``plan_advance``: helper de "avançar uma" — fecha a in_progress atual e
    abre a próxima pending.
  - ``plan_list``: lista todos os planos já criados.

O modelo deve usar ``plan_task`` quando o usuário pede algo grande tipo
"crie um SaaS X" / "faça o app Y do PRD ao deploy". Aí ele segue o plano
chamando outras tools (write/edit/bash/python_exec/etc) e marcando o progresso
com ``plan_set_status`` ou ``plan_advance``.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from mtzcode import orchestrator as _orch
from mtzcode.orchestrator.subagent import run_subagent
from mtzcode.tools.base import Tool, ToolError


# ----------------------------- plan_task ---------------------------------


class PlanTaskItem(BaseModel):
    content: str = Field(..., description="Descrição curta da tarefa.")
    status: str = Field(
        "pending",
        description="Status inicial: pending (default), in_progress, completed, skipped, blocked.",
    )


class PlanPhase(BaseModel):
    name: str = Field(..., description="Nome curto da fase, ex: 'Discovery', 'Backend', 'Deploy'.")
    description: str = Field("", description="Descrição opcional do que a fase entrega.")
    tasks: list[PlanTaskItem] = Field(
        ...,
        description="Tarefas concretas dessa fase. Cada uma deve ser acionável (verbo + alvo).",
    )


class PlanTaskArgs(BaseModel):
    goal: str = Field(
        ...,
        description="Objetivo macro do projeto, ex: 'SaaS de agendamento com login e Stripe'.",
    )
    phases: list[PlanPhase] = Field(
        ...,
        description=(
            "Fases do plano em ordem. Para projetos do PRD ao deploy use algo como: "
            "Discovery → Arquitetura → Backend → Frontend → Integrações → QA → Deploy. "
            "Cada fase precisa de tarefas concretas (3-8 por fase é o ideal)."
        ),
    )
    notes: str = Field("", description="Anotações livres opcionais (constraints, stack, prazo).")


class PlanTaskTool(Tool):
    name = "plan_task"
    destructive = False
    description = (
        "Cria um PLANO estruturado pra um pedido grande do usuário. Use SEMPRE que "
        "o pedido envolver construir/entregar algo de várias fases (PRD, app, plataforma, "
        "migração, refatoração grande). O plano vira o roteiro: você deve segui-lo "
        "executando uma tarefa por vez e marcando progresso com `plan_set_status` "
        "ou `plan_advance`. Sobrescreve o plano corrente e espelha na aba Tarefas da UI."
    )
    Args = PlanTaskArgs

    def run(self, args: PlanTaskArgs) -> str:  # type: ignore[override]
        try:
            phases = [p.model_dump() for p in args.phases]
            plan = _orch.create_plan(args.goal, phases, notes=args.notes)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return _format_plan(plan, header=f"plano criado: {plan['id']}")


# ----------------------------- plan_show ---------------------------------


class PlanShowArgs(BaseModel):
    plan_id: str | None = Field(
        None,
        description="ID do plano. Vazio = mostra o plano corrente.",
    )


class PlanShowTool(Tool):
    name = "plan_show"
    destructive = False
    description = (
        "Mostra o plano corrente (ou outro pelo id) com fases, tarefas e status. "
        "Use no início duma sessão pra ver onde parou, ou pra verificar o estado "
        "antes de avançar."
    )
    Args = PlanShowArgs

    def run(self, args: PlanShowArgs) -> str:  # type: ignore[override]
        if args.plan_id:
            plan = _orch.load_plan(args.plan_id)
            if not plan:
                raise ToolError(f"plano '{args.plan_id}' não encontrado")
        else:
            plan = _orch.current_plan()
            if not plan:
                return "(sem plano corrente — use `plan_task` pra criar um)"
        return _format_plan(plan)


# --------------------------- plan_set_status -----------------------------


class PlanSetStatusArgs(BaseModel):
    task_id: str = Field(
        ...,
        description="ID da task no formato pX.tY (ex: 'p2.t3' = fase 2, task 3).",
    )
    status: str = Field(
        ...,
        description="Novo status: pending, in_progress, completed, skipped, blocked.",
    )
    plan_id: str | None = Field(
        None,
        description="ID do plano (omita pra usar o corrente).",
    )


class PlanSetStatusTool(Tool):
    name = "plan_set_status"
    destructive = False
    description = (
        "Muda o status de uma task do plano. Use sempre que terminar/abandonar/"
        "começar uma task — assim o usuário vê o progresso na UI em tempo real. "
        "Mantenha exatamente UMA task como 'in_progress' por vez."
    )
    Args = PlanSetStatusArgs

    def run(self, args: PlanSetStatusArgs) -> str:  # type: ignore[override]
        plan_id = args.plan_id
        if not plan_id:
            cur = _orch.current_plan()
            if not cur:
                raise ToolError("não há plano corrente — crie um com plan_task")
            plan_id = cur["id"]
        try:
            plan = _orch.set_task_status(plan_id, args.task_id, args.status)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return _format_plan(plan, header=f"task {args.task_id} → {args.status}")


# ---------------------------- plan_advance -------------------------------


class PlanAdvanceArgs(BaseModel):
    pass


class PlanAdvanceTool(Tool):
    name = "plan_advance"
    destructive = False
    description = (
        "Avança o plano corrente uma posição: fecha a task 'in_progress' atual "
        "(marca completed) e abre a próxima 'pending'. Atalho pro fluxo "
        "linear — pra mudanças não-lineares use plan_set_status."
    )
    Args = PlanAdvanceArgs

    def run(self, args: PlanAdvanceArgs) -> str:  # type: ignore[override]
        plan = _orch.advance_current()
        if not plan:
            raise ToolError("não há plano corrente — crie um com plan_task")
        return _format_plan(plan, header="plano avançado")


# ----------------------------- plan_list ---------------------------------


class PlanListArgs(BaseModel):
    pass


class PlanListTool(Tool):
    name = "plan_list"
    destructive = False
    description = "Lista todos os planos já criados (id, objetivo, status, contagem)."
    Args = PlanListArgs

    def run(self, args: PlanListArgs) -> str:  # type: ignore[override]
        plans = _orch.list_plans()
        if not plans:
            return "(nenhum plano ainda)"
        cur = _orch.current_plan()
        cur_id = (cur or {}).get("id")
        lines = [f"{len(plans)} plano(s):"]
        for p in plans:
            mark = "★" if p.get("id") == cur_id else " "
            lines.append(
                f"  {mark} {p['id']}  [{p.get('status')}]  "
                f"{p.get('phases')}f/{p.get('tasks')}t  — {p.get('goal')}"
            )
        return "\n".join(lines)


# ----------------------------- spawn_agent -------------------------------


class SpawnAgentArgs(BaseModel):
    task: str = Field(
        ...,
        description=(
            "Tarefa CONCRETA e isolada pro sub-agente executar. Deve ser auto-contida: "
            "o sub-agente não vê o histórico do principal. Inclua os caminhos, nomes e "
            "contexto necessários direto na descrição."
        ),
    )
    role: str = Field(
        "executor",
        description=(
            "Papel do sub-agente em uma frase. Ex: 'pesquisador web', "
            "'implementador de backend Python', 'redator técnico', 'verificador de testes'."
        ),
    )
    tools: list[str] | None = Field(
        None,
        description=(
            "Subset de tools que o sub-agente pode usar (nomes exatos, ex: "
            "['read', 'write', 'bash']). Vazio = herda tudo do pai (menos planning/spawn). "
            "MENOS é melhor — escopo restrito = sub-agente mais focado e rápido."
        ),
    )
    max_iterations: int = Field(
        12,
        description="Limite de iterações do loop do sub-agente. Default 12, máx 25.",
        ge=1,
        le=25,
    )


class SpawnAgentTool(Tool):
    name = "spawn_agent"
    destructive = False
    description = (
        "Delega uma tarefa específica e isolada pra um SUB-AGENTE. Use quando: "
        "(a) a tarefa pode ser feita de forma auto-contida com poucos resultados; "
        "(b) explorar/pesquisar gastaria muito contexto no histórico principal; "
        "(c) você quer paralelizar / isolar trabalho de uma fase do plano. "
        "O sub-agente recebe só a `task` (sem ver seu histórico), executa com "
        "as tools que você listar, e devolve uma string curta com o resultado. "
        "NÃO use pra coisas triviais — o overhead de spin-up não compensa."
    )
    Args = SpawnAgentArgs

    def run(self, args: SpawnAgentArgs) -> str:  # type: ignore[override]
        try:
            outcome = run_subagent(
                task=args.task,
                role=args.role,
                tools=args.tools,
                max_iterations=args.max_iterations,
            )
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"spawn_agent falhou: {exc}") from exc
        meta = (
            f"[sub-agente terminou — depth={outcome['depth']}, "
            f"iters={outcome['iterations']}, tool_calls={outcome['tool_calls']}]"
        )
        result = outcome.get("result") or "(sub-agente devolveu vazio)"
        return f"{meta}\n{result}"


# ----------------------------- helpers -----------------------------------


def _format_plan(plan: dict[str, Any], *, header: str | None = None) -> str:
    from mtzcode.orchestrator.store import summarize_plan

    lines: list[str] = []
    if header:
        lines.append(header)
    s = summarize_plan(plan)
    lines.append(
        f"plano: {plan.get('id')}  [{plan.get('status')}]  "
        f"{s['completed']}/{s['total']} done  "
        f"({s['in_progress']} em andamento, {s['pending']} pendente)"
    )
    lines.append(f"objetivo: {plan.get('goal')}")
    if plan.get("notes"):
        lines.append(f"notas: {plan['notes']}")
    for phase in plan.get("phases") or []:
        ph_tasks = phase.get("tasks") or []
        ph_done = sum(1 for t in ph_tasks if t.get("status") in ("completed", "skipped"))
        lines.append("")
        lines.append(f"▸ {phase.get('id')} {phase.get('name')}  ({ph_done}/{len(ph_tasks)})")
        if phase.get("description"):
            lines.append(f"   {phase['description']}")
        for task in ph_tasks:
            icon = {
                "completed": "✓",
                "in_progress": "▶",
                "pending": "○",
                "skipped": "↷",
                "blocked": "✗",
            }.get(task.get("status"), "?")
            lines.append(f"   {icon} {task.get('id')}  {task.get('content')}")
    return "\n".join(lines)
