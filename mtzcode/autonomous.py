"""Modo autônomo do mtzcode.

Permite que o usuário passe uma tarefa e o agent trabalhe SEM pedir
confirmação para nada até terminar (ou bater num critério de parada).

Existe um bloqueio duro de comandos extremamente perigosos via
``DANGEROUS_PATTERNS`` — mesmo em auto mode, esses comandos NUNCA
são executados.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from rich.console import Console

from mtzcode.agent import Agent, AgentEvent, ConfirmCallback

# ---------------------------------------------------------------------------
# Lista negra de comandos catastróficos.
# ---------------------------------------------------------------------------
# Mesmo no modo autônomo, esses padrões fazem o auto_confirm devolver False.
# Use regex (re.IGNORECASE). Pensa: "se o modelo emitir isso, eu choro".
DANGEROUS_PATTERNS: list[str] = [
    r"rm\s+-rf\s+/(?:\s|$)",
    r"rm\s+-rf\s+~",
    r"rm\s+-rf\s+\$HOME",
    r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:",  # fork bomb
    r"\bmkfs\b",
    r"dd\s+if=.*\s+of=/dev/",
    r"sudo\s+rm\b",
    r"curl\b.*\|\s*(sh|bash)\b",
    r"wget\b.*\|\s*(sh|bash)\b",
    r"chmod\s+-R\s+777\s+/",
    r"git\s+push\b.*--force\b.*\bmain\b",
    r"git\s+push\b.*-f\b.*\bmain\b",
    r"git\s+reset\s+--hard\s+origin/main\b",
    r":\s*>\s*~/\.ssh\b",
]

_DANGEROUS_RE = [re.compile(p, re.IGNORECASE) for p in DANGEROUS_PATTERNS]

_console = Console()


# ---------------------------------------------------------------------------
# Detecção de comandos perigosos
# ---------------------------------------------------------------------------
def is_dangerous(tool_name: str, args: dict[str, Any]) -> tuple[bool, str]:
    """Verifica se a chamada de tool deve ser bloqueada mesmo em auto mode.

    Retorna ``(True, motivo)`` se for perigoso, ``(False, "")`` caso contrário.
    Foca especialmente na tool ``bash``, casando o ``command`` contra
    ``DANGEROUS_PATTERNS``.
    """
    if not isinstance(args, dict):
        return False, ""

    name = (tool_name or "").lower()

    # Para bash/shell, inspeciona o comando.
    if name in {"bash", "shell", "run_bash", "execute_bash"}:
        command = args.get("command") or args.get("cmd") or ""
        if isinstance(command, list):
            command = " ".join(str(x) for x in command)
        if not isinstance(command, str):
            return False, ""
        for pattern, regex in zip(DANGEROUS_PATTERNS, _DANGEROUS_RE):
            if regex.search(command):
                return True, f"comando casa padrão proibido: {pattern}"

    # Bloqueio extra: tool de escrita tentando estourar arquivos sensíveis.
    if name in {"write_file", "edit_file", "str_replace_editor"}:
        path = args.get("path") or args.get("file_path") or ""
        if isinstance(path, str):
            sensitive = ("/.ssh/", "/etc/passwd", "/etc/shadow")
            for s in sensitive:
                if s in path:
                    return True, f"escrita em caminho sensível: {s}"

    return False, ""


# ---------------------------------------------------------------------------
# Callback de auto-confirmação
# ---------------------------------------------------------------------------
def auto_confirm_factory(blocklist_enabled: bool = True) -> ConfirmCallback:
    """Constrói um ``ConfirmCallback`` que aprova tudo, exceto comandos
    listados em ``DANGEROUS_PATTERNS`` (quando ``blocklist_enabled``).
    """

    def _auto_confirm(tool_name: str, args: dict[str, Any]) -> bool:
        if blocklist_enabled:
            blocked, reason = is_dangerous(tool_name, args)
            if blocked:
                _console.print(
                    f"[red][auto-mode] BLOQUEADO[/] tool=[bold]{tool_name}[/] "
                    f"motivo: {reason}"
                )
                _console.print(f"[dim]args: {args}[/]")
                return False
        # Tudo liberado.
        return True

    return _auto_confirm


# ---------------------------------------------------------------------------
# Runner autônomo
# ---------------------------------------------------------------------------
class AutonomousRunner:
    """Executa o ``Agent`` em loop sem pedir confirmação humana.

    - Injeta ``auto_confirm_factory()`` no agent.
    - Aumenta ``max_iterations`` para algo proporcional ao trabalho.
    - Opcionalmente checa um ``goal_checker`` após cada resposta final;
      se o objetivo não foi atingido, manda um follow-up automático.
    - Limita follow-ups a 5 para evitar loops infinitos.
    - Restaura o estado original do agent ao final.
    """

    MAX_FOLLOWUPS = 5

    def __init__(
        self,
        agent: Agent,
        max_iterations: int = 200,
        goal_checker: Callable[[str], bool] | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self.agent = agent
        self.max_iterations = max_iterations
        self.goal_checker = goal_checker
        self.on_event = on_event

    def run(self, task: str) -> str:
        """Executa a tarefa em modo autônomo. Retorna o texto final."""
        # Salva estado pra restaurar depois.
        original_confirm = self.agent.confirm_cb
        original_max = self.agent.max_iterations

        self.agent.confirm_cb = auto_confirm_factory(blocklist_enabled=True)
        self.agent.max_iterations = max(self.max_iterations, original_max)

        try:
            _console.print(
                f"[bold cyan][auto-mode][/] iniciando tarefa "
                f"(max_iter={self.agent.max_iterations})"
            )

            final_text = self.agent.run_streaming(task, on_event=self.on_event)

            # Loop de follow-ups se temos um goal_checker.
            followups = 0
            while self.goal_checker is not None and followups < self.MAX_FOLLOWUPS:
                try:
                    achieved = bool(self.goal_checker(final_text))
                except Exception as exc:  # noqa: BLE001 — checker do usuário
                    _console.print(
                        f"[yellow][auto-mode] goal_checker explodiu: {exc}[/]"
                    )
                    break

                if achieved:
                    _console.print("[green][auto-mode] critério atingido[/]")
                    break

                followups += 1
                _console.print(
                    f"[yellow][auto-mode] objetivo ainda não atingido, "
                    f"follow-up {followups}/{self.MAX_FOLLOWUPS}[/]"
                )
                followup_msg = (
                    "ainda não terminou, continue: o critério de aceitação "
                    "ainda não foi satisfeito. Continue trabalhando na tarefa "
                    f"original até concluí-la.\n\nTarefa: {task}"
                )
                final_text = self.agent.run_streaming(
                    followup_msg, on_event=self.on_event
                )
            else:
                if (
                    self.goal_checker is not None
                    and followups >= self.MAX_FOLLOWUPS
                ):
                    _console.print(
                        "[red][auto-mode] limite de follow-ups atingido[/]"
                    )

            return final_text
        finally:
            # Restaura estado original do agent.
            self.agent.confirm_cb = original_confirm
            self.agent.max_iterations = original_max


# ---------------------------------------------------------------------------
# Goal checkers a partir de critérios declarativos
# ---------------------------------------------------------------------------
def load_goal_checker_from_criteria(
    criteria: list[str],
) -> Callable[[str], bool]:
    """Constrói um goal_checker a partir de uma lista de critérios.

    Formatos suportados (mínimo viável):
      - ``file_exists:<path>``  → ``Path(path).exists()``
      - ``cmd_zero:<cmd>``      → ``subprocess.run(cmd, shell=True).returncode == 0``

    O checker resultante retorna True somente se TODOS os critérios passarem.
    Critérios desconhecidos são ignorados (com aviso).
    """
    parsed: list[tuple[str, str]] = []
    for raw in criteria:
        if ":" not in raw:
            _console.print(f"[yellow]critério ignorado (sem prefixo): {raw}[/]")
            continue
        kind, _, value = raw.partition(":")
        kind = kind.strip().lower()
        value = value.strip()
        if kind not in {"file_exists", "cmd_zero"}:
            _console.print(f"[yellow]critério desconhecido: {kind}[/]")
            continue
        parsed.append((kind, value))

    def _check(_final_text: str) -> bool:
        for kind, value in parsed:
            if kind == "file_exists":
                if not Path(value).expanduser().exists():
                    _console.print(f"[dim]✗ file_exists:{value}[/]")
                    return False
                _console.print(f"[dim]✓ file_exists:{value}[/]")
            elif kind == "cmd_zero":
                try:
                    proc = subprocess.run(
                        value,
                        shell=True,
                        capture_output=True,
                        timeout=120,
                    )
                except subprocess.SubprocessError as exc:
                    _console.print(f"[dim]✗ cmd_zero:{value} ({exc})[/]")
                    return False
                if proc.returncode != 0:
                    _console.print(
                        f"[dim]✗ cmd_zero:{value} (rc={proc.returncode})[/]"
                    )
                    return False
                _console.print(f"[dim]✓ cmd_zero:{value}[/]")
        return True

    return _check
