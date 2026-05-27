"""Persistência de tarefas agendadas em ~/.mtzcode/schedules.json.

Cada tarefa é serializada como dict simples; o `TaskStore` faz o
load/save atômico do arquivo JSON inteiro.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

# Caminho padrão do arquivo de tarefas (~/.mtzcode/schedules.json).
DEFAULT_STORE_PATH = Path.home() / ".mtzcode" / "schedules.json"

# Tipos válidos para o comportamento de erro.
OnError = Literal["notify", "ignore", "stop"]


@dataclass
class ScheduledTask:
    """Representa uma tarefa agendada que será executada pelo daemon.

    Atributos:
        id: identificador único (uuid4 hex).
        name: nome amigável usado em logs e listagens.
        cron: expressão cron com 5 campos.
        prompt: prompt enviado ao agent quando a tarefa rodar.
        profile: nome do perfil de modelo (None = usa o default do config).
        auto_mode: se True, roda dentro do AutonomousRunner sem confirmação.
        on_error: o que fazer quando a execução falhar.
        enabled: se False, o daemon ignora a tarefa.
        created_at: timestamp ISO de criação.
        last_run: último timestamp ISO em que a tarefa rodou (ou None).
        last_status: resumo curto do último resultado ("ok", "erro: ...").
    """

    id: str
    name: str
    cron: str
    prompt: str
    profile: str | None = None
    auto_mode: bool = True
    on_error: OnError = "notify"
    enabled: bool = True
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_run: str | None = None
    last_status: str | None = None

    @classmethod
    def new(
        cls,
        name: str,
        cron: str,
        prompt: str,
        profile: str | None = None,
        auto_mode: bool = True,
        on_error: OnError = "notify",
        enabled: bool = True,
    ) -> "ScheduledTask":
        """Cria uma nova tarefa com id gerado automaticamente."""
        return cls(
            id=uuid.uuid4().hex[:12],
            name=name,
            cron=cron,
            prompt=prompt,
            profile=profile,
            auto_mode=auto_mode,
            on_error=on_error,
            enabled=enabled,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduledTask":
        # Filtra chaves desconhecidas pra não quebrar em upgrades futuros.
        allowed = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in allowed}
        return cls(**clean)


class TaskStore:
    """Wrapper sobre o arquivo JSON de tarefas agendadas."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_STORE_PATH

    # ------------------------------------------------------------------
    # IO básico
    # ------------------------------------------------------------------
    def _ensure_dir(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[ScheduledTask]:
        """Lê o arquivo e devolve a lista de tarefas. Retorna [] se inexistente."""
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Arquivo corrompido — não derruba o daemon, só ignora.
            return []
        if not isinstance(raw, list):
            return []
        tasks: list[ScheduledTask] = []
        for item in raw:
            if isinstance(item, dict):
                try:
                    tasks.append(ScheduledTask.from_dict(item))
                except TypeError:
                    continue
        return tasks

    def save(self, tasks: list[ScheduledTask]) -> None:
        """Salva atomicamente a lista inteira de tarefas no arquivo."""
        self._ensure_dir()
        payload = [t.to_dict() for t in tasks]
        # Escrita atômica via tempfile + os.replace pra evitar arquivo parcial.
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".schedules-", suffix=".json", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.path)
        except Exception:
            # Limpa o tempfile se algo der errado.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Operações CRUD
    # ------------------------------------------------------------------
    def add(self, task: ScheduledTask) -> None:
        tasks = self.load()
        tasks.append(task)
        self.save(tasks)

    def remove(self, task_id: str) -> bool:
        tasks = self.load()
        new_tasks = [t for t in tasks if t.id != task_id]
        if len(new_tasks) == len(tasks):
            return False
        self.save(new_tasks)
        return True

    def get(self, task_id: str) -> ScheduledTask | None:
        for t in self.load():
            if t.id == task_id:
                return t
        return None

    def update(self, task: ScheduledTask) -> bool:
        """Substitui a tarefa com o mesmo id. Retorna False se não existir."""
        tasks = self.load()
        found = False
        for i, t in enumerate(tasks):
            if t.id == task.id:
                tasks[i] = task
                found = True
                break
        if not found:
            return False
        self.save(tasks)
        return True
