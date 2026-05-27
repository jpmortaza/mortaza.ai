"""Daemon do scheduler + integração com launchd no macOS.

O `SchedulerDaemon` é um loop simples que acorda a cada 30 segundos,
recarrega o arquivo de tarefas e roda o que estiver `due`. A integração
com launchd permite instalar o daemon como LaunchAgent do usuário,
de forma que ele suba automaticamente no login.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from mtzcode.scheduler import runner
from mtzcode.scheduler.cron import is_due
from mtzcode.scheduler.store import ScheduledTask, TaskStore

# Intervalo do tick do loop principal (segundos).
TICK_SECONDS = 30

# Label padrão usado no plist do launchd.
DEFAULT_PLIST_LABEL = "com.mtzcode.scheduler"

# Caminho do plist no diretório padrão de LaunchAgents do usuário.
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

# Diretório de logs do daemon (stdout/stderr capturados pelo launchd).
LOG_DIR = Path.home() / ".mtzcode" / "logs"


class SchedulerDaemon:
    """Loop principal do scheduler.

    Uso típico (chamado pelo entrypoint `mtzcode daemon run`):

        SchedulerDaemon().run_forever()
    """

    def __init__(
        self,
        store: TaskStore | None = None,
        tick_seconds: int = TICK_SECONDS,
    ) -> None:
        self.store = store or TaskStore()
        self.tick_seconds = tick_seconds
        # Tasks marcadas como "stop" após erro fatal — pulamos elas até reload.
        self._stopped: set[str] = set()

    def run_forever(self) -> None:
        """Loop infinito até receber SIGTERM/SIGINT."""
        _log("daemon iniciado")
        try:
            while True:
                try:
                    self._tick()
                except Exception as exc:  # noqa: BLE001
                    _log(f"erro no tick: {exc}")
                time.sleep(self.tick_seconds)
        except KeyboardInterrupt:
            _log("daemon interrompido por KeyboardInterrupt")

    def _tick(self) -> None:
        """Uma iteração do loop: carrega tasks e roda o que estiver vencido."""
        now = datetime.now()
        tasks = self.store.load()
        for task in tasks:
            if not task.enabled:
                continue
            if task.id in self._stopped:
                continue

            last = _parse_iso(task.last_run)
            if not is_due(task.cron, last, now):
                continue

            _log(f"executando tarefa {task.id} ({task.name})")
            self._run_one(task)

    def _run_one(self, task: ScheduledTask) -> None:
        """Roda uma única tarefa e persiste o resultado de volta no store."""
        try:
            success, summary = runner.run_task(task)
        except Exception as exc:  # noqa: BLE001 — on_error="stop" rebenta aqui
            _log(f"tarefa {task.id} falhou de forma fatal: {exc}")
            self._stopped.add(task.id)
            task.last_run = datetime.now().isoformat()
            task.last_status = f"fatal: {exc}"
            self.store.update(task)
            return

        task.last_run = datetime.now().isoformat()
        task.last_status = ("ok: " if success else "erro: ") + summary[:200]
        self.store.update(task)
        _log(f"tarefa {task.id} -> {'ok' if success else 'erro'}")


# ----------------------------------------------------------------------
# Integração com launchd
# ----------------------------------------------------------------------
def install_launchd(plist_label: str = DEFAULT_PLIST_LABEL) -> Path:
    """Instala o daemon como LaunchAgent do usuário e o carrega.

    Retorna o caminho do plist criado. Idempotente: se já existir, reescreve
    e dá `unload` antes do `load` pra refletir mudanças.
    """
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    plist_path = LAUNCH_AGENTS_DIR / f"{plist_label}.plist"

    mtzcode_bin = _find_mtzcode_executable()
    plist_xml = _build_plist(plist_label, mtzcode_bin)
    plist_path.write_text(plist_xml, encoding="utf-8")

    # Se já tava carregado, descarrega antes (ignora erro).
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        check=False,
        capture_output=True,
    )
    subprocess.run(
        ["launchctl", "load", str(plist_path)],
        check=False,
        capture_output=True,
    )
    return plist_path


def uninstall_launchd(plist_label: str = DEFAULT_PLIST_LABEL) -> bool:
    """Descarrega e remove o plist. Retorna True se removeu algo."""
    plist_path = LAUNCH_AGENTS_DIR / f"{plist_label}.plist"
    if not plist_path.exists():
        return False
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        check=False,
        capture_output=True,
    )
    try:
        plist_path.unlink()
    except OSError:
        return False
    return True


def daemon_status(plist_label: str = DEFAULT_PLIST_LABEL) -> dict[str, object]:
    """Verifica se o daemon está ativo no launchd.

    Retorna um dict com chaves:
        installed: bool — plist existe em ~/Library/LaunchAgents
        loaded:    bool — launchctl list mostra o label
        pid:       int | None — PID atual, se rodando
    """
    plist_path = LAUNCH_AGENTS_DIR / f"{plist_label}.plist"
    installed = plist_path.exists()

    loaded = False
    pid: int | None = None
    try:
        proc = subprocess.run(
            ["launchctl", "list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in proc.stdout.splitlines():
            # Formato: "<pid>\t<status>\t<label>"
            parts = line.split("\t")
            if len(parts) >= 3 and parts[2].strip() == plist_label:
                loaded = True
                pid_str = parts[0].strip()
                if pid_str.isdigit():
                    pid = int(pid_str)
                break
    except (FileNotFoundError, subprocess.SubprocessError):
        pass

    return {"installed": installed, "loaded": loaded, "pid": pid}


# ----------------------------------------------------------------------
# Helpers internos
# ----------------------------------------------------------------------
def _build_plist(label: str, mtzcode_bin: str) -> str:
    """Gera o XML do plist do LaunchAgent."""
    stdout_log = LOG_DIR / "scheduler.out.log"
    stderr_log = LOG_DIR / "scheduler.err.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{mtzcode_bin}</string>
        <string>daemon</string>
        <string>run</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{stdout_log}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_log}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
"""


def _find_mtzcode_executable() -> str:
    """Tenta localizar o binário `mtzcode` no PATH; cai pro python -m como fallback."""
    found = shutil.which("mtzcode")
    if found:
        return found
    # Fallback: python -m mtzcode (não funciona como ProgramArguments único,
    # mas registramos só o python; o daemon plist usa apenas a primeira string.
    # Aqui devolvemos o binário python atual e o caller pode adaptar se quiser).
    return sys.executable


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _log(msg: str) -> None:
    """Log simples pra stderr — capturado pelo launchd no scheduler.err.log."""
    ts = datetime.now().isoformat(timespec="seconds")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


# Permite rodar o daemon diretamente: `python -m mtzcode.scheduler.daemon`.
if __name__ == "__main__":  # pragma: no cover
    os.makedirs(LOG_DIR, exist_ok=True)
    SchedulerDaemon().run_forever()
