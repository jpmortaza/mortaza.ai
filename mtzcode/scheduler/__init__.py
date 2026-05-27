"""Sistema de agendamento de tarefas do mtzcode.

Permite agendar prompts pra rodar automaticamente em horários definidos
por uma expressão cron, sem nenhuma interação do usuário.

Componentes:
    - ScheduledTask / TaskStore: persistência das tarefas em JSON.
    - cron: parser de expressões cron (usa croniter se disponível).
    - runner: executa uma tarefa via Agent + AutonomousRunner.
    - daemon: loop principal + integração com launchd no macOS.
    - cli_commands: helpers chamados pelos subcommands typer (não inclusos aqui).
"""
from __future__ import annotations

from mtzcode.scheduler.daemon import (
    SchedulerDaemon,
    daemon_status,
    install_launchd,
    uninstall_launchd,
)
from mtzcode.scheduler.store import ScheduledTask, TaskStore

__all__ = [
    "ScheduledTask",
    "TaskStore",
    "SchedulerDaemon",
    "install_launchd",
    "uninstall_launchd",
    "daemon_status",
]
