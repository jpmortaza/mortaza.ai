"""Entrada de linha de comando do modo autônomo.

Esta função é chamada por um comando typer (a ser adicionado em ``cli.py``
posteriormente). Aqui só montamos: Config + ChatClient + Agent + Registry e
disparamos o ``AutonomousRunner``.
"""
from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel

from mtzcode.agent import Agent, AgentEvent
from mtzcode.autonomous import (
    AutonomousRunner,
    load_goal_checker_from_criteria,
)
from mtzcode.client import ChatClient, ChatClientError
from mtzcode.config import Config
from mtzcode.profiles import get_profile
from mtzcode.tools import default_registry

_console = Console()


def run_auto(
    task: str,
    criteria: list[str] | None = None,
    profile_name: str | None = None,
) -> str:
    """Executa uma tarefa em modo autônomo.

    Parâmetros:
      - ``task``: descrição da tarefa em linguagem natural.
      - ``criteria``: lista opcional de critérios (ex: ``file_exists:foo.py``,
        ``cmd_zero:pytest -q``). Se passado, vira o ``goal_checker``.
      - ``profile_name``: nome de profile específico para forçar (opcional).

    Retorna o texto final emitido pelo agent.
    """
    cfg = Config.load()
    if profile_name:
        try:
            cfg = cfg.with_profile(get_profile(profile_name))
        except Exception as exc:  # noqa: BLE001
            _console.print(f"[red]profile inválido:[/] {exc}")
            raise

    registry = default_registry()

    try:
        client = ChatClient(cfg.profile, cfg.request_timeout_s)
    except ChatClientError as exc:
        _console.print(f"[red]erro ao iniciar cliente:[/] {exc}")
        raise

    agent = Agent(
        client=client,
        registry=registry,
        system_prompt=cfg.system_prompt(),
    )

    _console.print(
        Panel.fit(
            f"[bold]tarefa:[/] {task}\n"
            f"[bold]profile:[/] {cfg.profile.label}\n"
            f"[bold]critérios:[/] {criteria or '—'}",
            title="[cyan]mtzcode auto-mode[/]",
            border_style="cyan",
        )
    )

    goal_checker = (
        load_goal_checker_from_criteria(criteria) if criteria else None
    )

    def _on_event(event: AgentEvent) -> None:
        # Render minimalista de progresso para o auto-mode.
        kind = event.kind
        data: dict[str, Any] = event.data or {}
        if kind == "text_delta":
            _console.print(data.get("delta", ""), end="", soft_wrap=True)
        elif kind == "assistant_text_end":
            _console.print()  # quebra de linha
        elif kind == "tool_call":
            _console.print(
                f"\n[bold magenta]▶ tool[/] {data.get('name')} "
                f"[dim]{data.get('args')}[/]"
            )
        elif kind == "tool_result":
            result = str(data.get("result", ""))
            preview = result if len(result) < 400 else result[:400] + "…"
            _console.print(f"[green]✓[/] {preview}")
        elif kind == "tool_error":
            _console.print(f"[red]✗ erro tool {data.get('name')}:[/] {data.get('error')}")
        elif kind == "tool_denied":
            _console.print(
                f"[red]✗ tool bloqueada (auto-mode):[/] {data.get('name')}"
            )
        elif kind == "max_iterations":
            _console.print(
                f"[yellow]⚠ limite de iterações: {data.get('limit')}[/]"
            )

    runner = AutonomousRunner(
        agent=agent,
        max_iterations=200,
        goal_checker=goal_checker,
        on_event=_on_event,
    )

    final_text = runner.run(task)

    _console.print(
        Panel.fit(
            "[bold green]auto-mode finalizado[/]",
            border_style="green",
        )
    )
    return final_text
