"""Helpers chamados pelos subcommands typer do scheduler.

A CLI principal (`mtzcode/cli.py`) deve importar essas funções e expor
como subcommands sem precisar conhecer detalhes internos do store.
"""
from __future__ import annotations

from mtzcode.scheduler import runner
from mtzcode.scheduler.store import OnError, ScheduledTask, TaskStore


def add_task(
    name: str,
    cron: str,
    prompt: str,
    profile: str | None = None,
    auto_mode: bool = True,
    on_error: OnError = "notify",
) -> str:
    """Cria uma nova tarefa agendada e persiste no store.

    Retorna o id gerado.
    """
    store = TaskStore()
    task = ScheduledTask.new(
        name=name,
        cron=cron,
        prompt=prompt,
        profile=profile,
        auto_mode=auto_mode,
        on_error=on_error,
    )
    store.add(task)
    return task.id


def list_tasks() -> list[ScheduledTask]:
    """Devolve todas as tarefas registradas, na ordem em que estão no arquivo."""
    return TaskStore().load()


def remove_task(task_id: str) -> bool:
    """Remove uma tarefa pelo id. True se removeu, False se não existia."""
    return TaskStore().remove(task_id)


def run_task_now(task_id: str) -> tuple[bool, str]:
    """Executa imediatamente a tarefa, ignorando o cron.

    Útil pra testar uma tarefa recém-criada sem esperar o próximo disparo.
    Atualiza last_run/last_status no store também.
    """
    store = TaskStore()
    task = store.get(task_id)
    if task is None:
        return False, f"tarefa {task_id} não encontrada"

    success, summary = runner.run_task(task)

    # Persiste o resultado no store.
    from datetime import datetime

    task.last_run = datetime.now().isoformat()
    task.last_status = ("ok: " if success else "erro: ") + summary[:200]
    store.update(task)

    return success, summary


def enable_task(task_id: str, enabled: bool = True) -> bool:
    """Liga/desliga uma tarefa sem removê-la. True se atualizou."""
    store = TaskStore()
    task = store.get(task_id)
    if task is None:
        return False
    task.enabled = enabled
    return store.update(task)
