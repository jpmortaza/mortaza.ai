"""TODO list persistente — usado pelo modelo pra dividir tarefas longas.

Mantém uma lista de tarefas em ``~/.mtzcode/todos/current.json``. O modelo
chama a tool ``todo_write`` (ver ``mtzcode.tools.todo``) com a lista
inteira a cada atualização — o arquivo é sobrescrito atomicamente.

A UI web puxa via ``/api/todos`` e mostra na aba "Tarefas" do painel
direito, com checkboxes que evoluem em tempo real.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

TODO_DIR = Path.home() / ".mtzcode" / "todos"
CURRENT_FILE = TODO_DIR / "current.json"

Status = Literal["pending", "in_progress", "completed"]
VALID_STATUS = ("pending", "in_progress", "completed")


def _ensure_dir() -> None:
    TODO_DIR.mkdir(parents=True, exist_ok=True)


def load_todos() -> dict[str, Any]:
    """Carrega a lista atual. Devolve dict com 'todos' e 'updated_at'."""
    if not CURRENT_FILE.exists():
        return {"todos": [], "updated_at": None}
    try:
        data = json.loads(CURRENT_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            # Formato antigo — só lista
            return {"todos": data, "updated_at": None}
        if not isinstance(data, dict):
            return {"todos": [], "updated_at": None}
        return {
            "todos": data.get("todos") or [],
            "updated_at": data.get("updated_at"),
        }
    except (OSError, json.JSONDecodeError):
        return {"todos": [], "updated_at": None}


def save_todos(todos: list[dict[str, Any]]) -> dict[str, Any]:
    """Sobrescreve a lista. Valida cada item.

    Retorna o estado salvo: ``{todos, updated_at, summary}``.
    """
    _ensure_dir()
    cleaned: list[dict[str, Any]] = []
    for i, raw in enumerate(todos or []):
        if not isinstance(raw, dict):
            raise ValueError(f"item #{i + 1} não é um objeto")
        content = (raw.get("content") or "").strip()
        if not content:
            raise ValueError(f"item #{i + 1}: 'content' é obrigatório")
        status = raw.get("status") or "pending"
        if status not in VALID_STATUS:
            raise ValueError(
                f"item #{i + 1}: status inválido '{status}'. "
                f"Use: {', '.join(VALID_STATUS)}"
            )
        item_id = raw.get("id") or f"t{i + 1}"
        cleaned.append(
            {
                "id": str(item_id),
                "content": content,
                "status": status,
            }
        )

    payload = {
        "todos": cleaned,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    # Escrita atômica: tmp + rename
    fd, tmp_path = tempfile.mkstemp(prefix=".todos-", dir=str(TODO_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, CURRENT_FILE)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    payload["summary"] = summarize(cleaned)
    return payload


def clear_todos() -> None:
    """Apaga a lista atual."""
    if CURRENT_FILE.exists():
        try:
            CURRENT_FILE.unlink()
        except OSError:
            pass


def summarize(todos: list[dict[str, Any]]) -> dict[str, int]:
    """Conta itens por status."""
    counts = {"pending": 0, "in_progress": 0, "completed": 0, "total": 0}
    for t in todos or []:
        s = t.get("status") or "pending"
        if s in counts:
            counts[s] += 1
        counts["total"] += 1
    return counts
