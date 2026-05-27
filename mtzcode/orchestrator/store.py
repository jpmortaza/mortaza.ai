"""Storage de planos do orquestrador.

Cada plano é um JSON em ``~/.mtzcode/orchestrator/plans/<id>.json``::

    {
      "id": "plan-20260407-153012",
      "goal": "Criar SaaS de agendamento com login e pagamento",
      "created_at": "2026-04-07T15:30:12",
      "updated_at": "...",
      "status": "active",     # active | done | abandoned
      "phases": [
        {
          "name": "Discovery & PRD",
          "tasks": [
            {"id": "p1.t1", "content": "definir personas", "status": "completed"},
            {"id": "p1.t2", "content": "user stories", "status": "in_progress"}
          ]
        },
        ...
      ]
    }

Um plano de cada vez é "current" (apontado por ``current.txt``). A TODO list
(``mtzcode.todos``) é o **espelho** do plano corrente — o painel "Tarefas" da
UI mostra o plano automaticamente.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from mtzcode import todos as _todos

PLAN_DIR = Path.home() / ".mtzcode" / "orchestrator" / "plans"
CURRENT_POINTER = PLAN_DIR / "current.txt"

VALID_TASK_STATUS = ("pending", "in_progress", "completed", "skipped", "blocked")
VALID_PLAN_STATUS = ("active", "done", "abandoned")


def _ensure_dir() -> None:
    PLAN_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(prefix=".plan-", dir=str(PLAN_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _normalize_phases(phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Valida e numera as fases/tarefas, gerando ids estáveis."""
    if not phases:
        raise ValueError("plano precisa de pelo menos 1 fase")
    out: list[dict[str, Any]] = []
    for pi, phase in enumerate(phases, start=1):
        if not isinstance(phase, dict):
            raise ValueError(f"fase #{pi} não é objeto")
        name = (phase.get("name") or "").strip()
        if not name:
            raise ValueError(f"fase #{pi}: 'name' é obrigatório")
        raw_tasks = phase.get("tasks") or []
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise ValueError(f"fase '{name}': precisa de pelo menos 1 task")
        tasks_out: list[dict[str, Any]] = []
        for ti, raw_t in enumerate(raw_tasks, start=1):
            if isinstance(raw_t, str):
                content = raw_t.strip()
                status = "pending"
            elif isinstance(raw_t, dict):
                content = (raw_t.get("content") or "").strip()
                status = raw_t.get("status") or "pending"
            else:
                raise ValueError(
                    f"fase '{name}' task #{ti}: precisa ser string ou objeto"
                )
            if not content:
                raise ValueError(f"fase '{name}' task #{ti}: 'content' obrigatório")
            if status not in VALID_TASK_STATUS:
                raise ValueError(
                    f"fase '{name}' task #{ti}: status inválido '{status}'. "
                    f"Use: {', '.join(VALID_TASK_STATUS)}"
                )
            tasks_out.append(
                {
                    "id": f"p{pi}.t{ti}",
                    "content": content,
                    "status": status,
                }
            )
        out.append(
            {
                "id": f"p{pi}",
                "name": name,
                "description": (phase.get("description") or "").strip(),
                "tasks": tasks_out,
            }
        )
    return out


def _new_id() -> str:
    return "plan-" + datetime.now().strftime("%Y%m%d-%H%M%S")


def create_plan(
    goal: str,
    phases: list[dict[str, Any]],
    *,
    notes: str = "",
    set_current: bool = True,
) -> dict[str, Any]:
    """Cria um plano novo, persiste e (default) marca como current.

    Espelha automaticamente para a TODO list pra UI mostrar.
    """
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("goal é obrigatório")
    normalized = _normalize_phases(phases)
    plan_id = _new_id()
    payload: dict[str, Any] = {
        "id": plan_id,
        "goal": goal,
        "notes": notes.strip(),
        "status": "active",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "phases": normalized,
    }
    _atomic_write(PLAN_DIR / f"{plan_id}.json", payload)
    if set_current:
        _ensure_dir()
        CURRENT_POINTER.write_text(plan_id, encoding="utf-8")
        mirror_to_todos(payload)
    return payload


def load_plan(plan_id: str) -> dict[str, Any] | None:
    path = PLAN_DIR / f"{plan_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def current_plan() -> dict[str, Any] | None:
    if not CURRENT_POINTER.exists():
        return None
    try:
        plan_id = CURRENT_POINTER.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not plan_id:
        return None
    return load_plan(plan_id)


def list_plans() -> list[dict[str, Any]]:
    """Lista resumida de todos os planos (mais recente primeiro)."""
    if not PLAN_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in PLAN_DIR.glob("plan-*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append(
            {
                "id": data.get("id"),
                "goal": data.get("goal"),
                "status": data.get("status"),
                "updated_at": data.get("updated_at"),
                "phases": len(data.get("phases") or []),
                "tasks": sum(len(p.get("tasks") or []) for p in data.get("phases") or []),
            }
        )
    out.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return out


def _save(plan: dict[str, Any]) -> dict[str, Any]:
    plan["updated_at"] = _now_iso()
    _atomic_write(PLAN_DIR / f"{plan['id']}.json", plan)
    cur = current_plan()
    if cur and cur.get("id") == plan.get("id"):
        mirror_to_todos(plan)
    return plan


def set_task_status(
    plan_id: str,
    task_id: str,
    status: str,
) -> dict[str, Any]:
    if status not in VALID_TASK_STATUS:
        raise ValueError(
            f"status inválido '{status}'. Use: {', '.join(VALID_TASK_STATUS)}"
        )
    plan = load_plan(plan_id)
    if not plan:
        raise ValueError(f"plano '{plan_id}' não encontrado")
    found = False
    for phase in plan.get("phases") or []:
        for task in phase.get("tasks") or []:
            if task.get("id") == task_id:
                task["status"] = status
                found = True
                break
        if found:
            break
    if not found:
        raise ValueError(f"task '{task_id}' não encontrada em '{plan_id}'")
    # Se todas as tasks viraram completed/skipped, marca o plano como done.
    if _all_tasks_terminal(plan):
        plan["status"] = "done"
    return _save(plan)


def advance_current() -> dict[str, Any] | None:
    """Marca a primeira ``in_progress`` como completed e a próxima ``pending``
    como ``in_progress``. Retorna o plano atualizado ou None se não há current.
    """
    plan = current_plan()
    if not plan:
        return None
    # Acha a in_progress atual e fecha; depois acha a próxima pending e abre.
    closed = False
    for phase in plan.get("phases") or []:
        for task in phase.get("tasks") or []:
            if not closed and task.get("status") == "in_progress":
                task["status"] = "completed"
                closed = True
                continue
            if closed and task.get("status") == "pending":
                task["status"] = "in_progress"
                if _all_tasks_terminal(plan):
                    plan["status"] = "done"
                return _save(plan)
    # Não tinha in_progress: abre a primeira pending
    if not closed:
        for phase in plan.get("phases") or []:
            for task in phase.get("tasks") or []:
                if task.get("status") == "pending":
                    task["status"] = "in_progress"
                    return _save(plan)
    if _all_tasks_terminal(plan):
        plan["status"] = "done"
    return _save(plan)


def _all_tasks_terminal(plan: dict[str, Any]) -> bool:
    terminal = {"completed", "skipped"}
    for phase in plan.get("phases") or []:
        for task in phase.get("tasks") or []:
            if task.get("status") not in terminal:
                return False
    return True


def mirror_to_todos(plan: dict[str, Any]) -> None:
    """Espelha o plano (achatado) na TODO list — UI mostra automaticamente.

    Cada item vira ``[fase] tarefa`` para o usuário ver a estrutura.
    """
    flat: list[dict[str, Any]] = []
    for phase in plan.get("phases") or []:
        phase_name = phase.get("name") or "fase"
        for task in phase.get("tasks") or []:
            status = task.get("status") or "pending"
            # TODO list só conhece pending/in_progress/completed
            if status == "skipped":
                status = "completed"
            elif status == "blocked":
                status = "in_progress"
            flat.append(
                {
                    "id": task.get("id"),
                    "content": f"[{phase_name}] {task.get('content')}",
                    "status": status,
                }
            )
    try:
        _todos.save_todos(flat)
    except ValueError:
        # Plano malformado não deveria quebrar persistência
        pass


def summarize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    counts = {"total": 0, "completed": 0, "in_progress": 0, "pending": 0, "blocked": 0, "skipped": 0}
    for phase in plan.get("phases") or []:
        for task in phase.get("tasks") or []:
            counts["total"] += 1
            s = task.get("status") or "pending"
            if s in counts:
                counts[s] += 1
    return counts
