"""Execução real de uma tarefa agendada.

Monta um Agent + ChatClient + ToolRegistry, envolve em AutonomousRunner
(import lazy pra evitar dependência circular / hard dependency) e roda
o prompt da tarefa. Retorna (sucesso, resumo curto).
"""
from __future__ import annotations

import shlex
import subprocess
import traceback

from mtzcode.scheduler.store import ScheduledTask

# Tamanho máximo do resumo gravado em last_status (evita JSON gigante).
_SUMMARY_MAX_LEN = 500


def run_task(task: ScheduledTask) -> tuple[bool, str]:
    """Executa a tarefa e devolve `(success, resumo)`.

    Em caso de erro, respeita `task.on_error`:
        - "notify": dispara notificação macOS via osascript.
        - "ignore": engole o erro silenciosamente.
        - "stop":   propaga (caller decide o que fazer; o daemon usa pra parar
                    de tentar essa task até reload).
    """
    try:
        return _do_run(task)
    except Exception as exc:  # noqa: BLE001 — queremos pegar tudo aqui mesmo
        tb = traceback.format_exc()
        msg = f"erro: {exc}"
        if task.on_error == "notify":
            _notify_macos(
                f"mtzcode: tarefa '{task.name}' falhou", str(exc)[:200]
            )
        elif task.on_error == "stop":
            # Re-lança pra o daemon poder marcar a task como travada.
            raise
        # "ignore" ou "notify": só devolve o erro.
        return False, _trim(msg + "\n" + tb)


def _do_run(task: ScheduledTask) -> tuple[bool, str]:
    """Faz a execução de fato. Pode levantar — `run_task` cuida do try/except."""
    # Imports tardios pra evitar ciclos e pra não pagar custo de import
    # quando o módulo é só inspecionado (ex.: listar tarefas).
    from mtzcode.client import ChatClient
    from mtzcode.config import Config
    from mtzcode.profiles import get_profile
    from mtzcode.tools.registry import build_default_registry  # type: ignore

    config = Config.load()

    # Aplica o profile da tarefa, se houver.
    if task.profile:
        try:
            profile = get_profile(task.profile)
            config = config.with_profile(profile)
        except KeyError as exc:
            return False, f"erro: {exc}"

    # Constrói o ChatClient. A assinatura exata varia entre versões; tentamos
    # algumas formas comuns sem quebrar se uma não casar.
    client = _build_client(ChatClient, config)

    # Registry de tools — usa o builder padrão se existir, senão cria vazio.
    try:
        registry = build_default_registry()
    except Exception:
        from mtzcode.tools.base import ToolRegistry  # fallback

        registry = ToolRegistry()

    system_prompt = config.system_prompt()

    # Cria o Agent. Sem confirm_cb porque é execução headless.
    from mtzcode.agent import Agent

    agent = Agent(
        client=client,
        registry=registry,
        system_prompt=system_prompt,
        confirm_cb=None,
    )

    # Envolve em AutonomousRunner se a task pedir auto_mode.
    # Import lazy: o módulo `mtzcode.autonomous` pode não estar carregado.
    final_text: str
    if task.auto_mode:
        try:
            from mtzcode.autonomous import AutonomousRunner  # type: ignore

            runner = AutonomousRunner(agent)
            final_text = runner.run(task.prompt)
        except ImportError:
            # Sem AutonomousRunner — degrade graceful pro Agent direto.
            final_text = agent.run(task.prompt)
    else:
        final_text = agent.run(task.prompt)

    summary = _trim(final_text or "(sem resposta)")
    return True, summary


def _build_client(client_cls, config) -> object:
    """Tenta instanciar o ChatClient com diferentes assinaturas conhecidas."""
    profile = config.profile
    # Tentativa 1: kwargs explícitos modernos.
    try:
        return client_cls(
            base_url=profile.base_url,
            model=profile.model,
            api_key_env=profile.api_key_env,
            timeout=config.request_timeout_s,
        )
    except TypeError:
        pass
    # Tentativa 2: passa o profile inteiro.
    try:
        return client_cls(profile=profile, timeout=config.request_timeout_s)
    except TypeError:
        pass
    # Tentativa 3: só o config.
    return client_cls(config)


def _notify_macos(title: str, message: str) -> None:
    """Dispara uma notificação nativa via osascript. Silencioso em falha."""
    # Escapa aspas duplas dos argumentos pra não quebrar o AppleScript.
    safe_title = title.replace('"', '\\"')
    safe_msg = message.replace('"', '\\"')
    script = f'display notification "{safe_msg}" with title "{safe_title}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        # Não é macOS ou osascript indisponível — ignora.
        pass


def _trim(text: str) -> str:
    """Corta strings muito longas pra caber confortavelmente em last_status."""
    text = text.strip()
    if len(text) <= _SUMMARY_MAX_LEN:
        return text
    return text[: _SUMMARY_MAX_LEN - 3] + "..."


# Helper exposto pra testes manuais via shell.
def _debug_cmd(cmd: str) -> str:
    """Útil pra logging — devolve o comando shell-escaped."""
    return " ".join(shlex.quote(p) for p in cmd.split())
