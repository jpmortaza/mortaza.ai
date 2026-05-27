"""Logs estruturados de sessão do mtzcode.

Cada execução do CLI grava um arquivo `.jsonl` em `~/.mtzcode/logs/` contendo
um evento por linha. Isso vira histórico replayável e debug fácil — dá pra
abrir depois com qualquer ferramenta de jq/grep ou recarregar via
`load_session`.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from mtzcode.agent import AgentEvent, EventCallback


# Diretório padrão pra logs de sessão.
DEFAULT_LOG_DIR = Path.home() / ".mtzcode" / "logs"


class SessionLogger:
    """Escreve eventos do agent em um arquivo JSONL por sessão.

    Uso típico:

        with SessionLogger() as logger:
            logger.log_user("oi")
            agent.run("oi", on_event=make_event_callback(logger))
    """

    def __init__(self, log_dir: Path | None = None) -> None:
        self.log_dir = Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        short_uuid = uuid.uuid4().hex[:8]
        self.path = self.log_dir / f"session-{timestamp}-{short_uuid}.jsonl"

        # Abre em append textual com flush manual após cada linha.
        self._fh = self.path.open("a", encoding="utf-8")
        self._closed = False

        # Marca o início da sessão pra facilitar parsing posterior.
        self._write_record(
            {
                "ts": _now_iso(),
                "kind": "session_start",
                "data": {"path": str(self.path)},
            }
        )

    # ------------------------------------------------------------------
    # API principal
    # ------------------------------------------------------------------
    def log_event(self, event: AgentEvent) -> None:
        """Persiste um AgentEvent emitido pelo agent loop."""
        self._write_record(
            {
                "ts": _now_iso(),
                "kind": event.kind,
                "data": _safe_data(event.data),
            }
        )

    def log_user(self, text: str) -> None:
        """Registra uma mensagem do usuário (input do prompt)."""
        self._write_record(
            {
                "ts": _now_iso(),
                "kind": "user_message",
                "data": {"text": text},
            }
        )

    def log_meta(self, key: str, value: Any) -> None:
        """Registra metadados arbitrários (ex: modelo usado, profile, cwd)."""
        self._write_record(
            {
                "ts": _now_iso(),
                "kind": "meta",
                "data": {"key": key, "value": _safe_value(value)},
            }
        )

    def close(self) -> None:
        """Fecha o arquivo. Idempotente."""
        if self._closed:
            return
        try:
            self._write_record(
                {
                    "ts": _now_iso(),
                    "kind": "session_end",
                    "data": {},
                }
            )
        finally:
            try:
                self._fh.close()
            finally:
                self._closed = True

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------
    def __enter__(self) -> SessionLogger:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------
    def _write_record(self, record: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            line = json.dumps(record, ensure_ascii=False)
        except (TypeError, ValueError):
            # Última cartada: força str() em tudo.
            line = json.dumps(
                {
                    "ts": record.get("ts", _now_iso()),
                    "kind": record.get("kind", "unknown"),
                    "data": {"_repr": str(record.get("data"))},
                },
                ensure_ascii=False,
            )
        self._fh.write(line + "\n")
        self._fh.flush()


# ----------------------------------------------------------------------
# Helpers de callback / leitura
# ----------------------------------------------------------------------
def make_event_callback(
    logger: SessionLogger,
    next_cb: EventCallback | None = None,
) -> EventCallback:
    """Cria um callback que loga o evento e em seguida chama `next_cb`.

    Útil pra encadear o logger com o renderer existente do CLI/web sem ter
    que mexer no agent.
    """

    def _cb(event: AgentEvent) -> None:
        try:
            logger.log_event(event)
        except Exception:
            # Logging nunca deve quebrar o loop do agent.
            pass
        if next_cb is not None:
            next_cb(event)

    return _cb


def cleanup_old_sessions(
    log_dir: Path | None = None,
    *,
    max_age_days: int = 60,
    max_files: int = 500,
) -> dict[str, Any]:
    """Remove logs antigos do diretório de sessão.

    Critérios (ambos aplicados):
      - Apaga arquivos `session-*.jsonl` mais velhos que ``max_age_days``.
      - Mantém no máximo ``max_files`` arquivos (mais recentes vencem).

    Retorna dict com `removed: int` e `kept: int`. Falhas individuais
    são engolidas pra não quebrar startup.
    """
    import time as _time

    base = Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
    if not base.exists():
        return {"removed": 0, "kept": 0}

    cutoff = _time.time() - (max_age_days * 86400)
    files = sorted(
        base.glob("session-*.jsonl"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    removed = 0
    survivors: list[Path] = []
    for idx, p in enumerate(files):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        # Excede limite de quantidade ou está muito antigo? Remove.
        if idx >= max_files or mtime < cutoff:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
        else:
            survivors.append(p)
    return {"removed": removed, "kept": len(survivors)}


def list_sessions(log_dir: Path | None = None) -> list[dict]:
    """Lista as sessões disponíveis no diretório de logs.

    Lê os primeiros eventos de cada arquivo pra extrair a primeira mensagem
    do usuário (vira "preview") e o timestamp inicial. Retorna ordenado do
    mais recente pro mais antigo.
    """
    base = Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
    if not base.exists():
        return []

    sessions: list[dict[str, Any]] = []
    for path in sorted(base.glob("session-*.jsonl"), reverse=True):
        info: dict[str, Any] = {
            "path": str(path),
            "name": path.name,
            "ts": None,
            "first_user_message": None,
        }
        try:
            with path.open("r", encoding="utf-8") as fh:
                # Olha só os primeiros 20 eventos pra não ler arquivos enormes.
                for i, line in enumerate(fh):
                    if i >= 20:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if info["ts"] is None:
                        info["ts"] = record.get("ts")
                    if (
                        info["first_user_message"] is None
                        and record.get("kind") == "user_message"
                    ):
                        data = record.get("data") or {}
                        info["first_user_message"] = data.get("text")
                        break
        except OSError:
            continue
        sessions.append(info)
    return sessions


def load_session(path: Path) -> list[dict]:
    """Carrega todos os eventos de um arquivo .jsonl de sessão."""
    p = Path(path)
    events: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # Linha corrompida — ignora silenciosamente.
                continue
    return events


def events_to_history(events: list[dict]) -> list[dict[str, Any]]:
    """Converte eventos de sessão num ``history`` pronto pro Agent.

    Pega só user_message + done/assistant_text + tool_result. text_delta
    e tool_call são ignorados (são intermediários, ``done`` já tem o texto
    final). Reutilizado pelo /api/sessions/{id}/resume e pelo CLI --resume.
    """
    history: list[dict[str, Any]] = []
    for ev in events:
        kind = ev.get("kind")
        data = ev.get("data") or {}
        if kind == "user_message":
            text = data.get("text", "")
            if text:
                history.append({"role": "user", "content": text})
        elif kind == "assistant_text":
            text = data.get("text", "")
            if text:
                history.append({"role": "assistant", "content": text})
        elif kind == "done":
            text = data.get("text", "")
            if text and (
                not history
                or history[-1].get("content") != text
                or history[-1].get("role") != "assistant"
            ):
                history.append({"role": "assistant", "content": text})
        elif kind == "tool_result":
            name = data.get("name", "")
            result = data.get("result", "")
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{name}",
                    "name": name,
                    "content": str(result),
                }
            )
    return history


def latest_session_for_cwd(
    cwd: str | Path,
    log_dir: Path | None = None,
) -> dict | None:
    """Devolve o info da sessão mais recente cuja meta 'cwd' bate com o cwd.

    Se nenhuma sessão tiver meta cwd salva, devolve a mais recente em geral
    como fallback (menos preciso, mas melhor que nada).
    """
    base = Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
    if not base.exists():
        return None
    target = str(Path(cwd).expanduser().resolve())
    fallback: dict | None = None
    for path in sorted(base.glob("session-*.jsonl"), reverse=True):
        try:
            with path.open("r", encoding="utf-8") as fh:
                meta_cwd: str | None = None
                first_user: str | None = None
                ts: str | None = None
                for i, line in enumerate(fh):
                    if i >= 30:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ts is None:
                        ts = record.get("ts")
                    if record.get("kind") == "meta":
                        d = record.get("data") or {}
                        if d.get("key") == "cwd":
                            meta_cwd = str(d.get("value") or "")
                    if (
                        first_user is None
                        and record.get("kind") == "user_message"
                    ):
                        first_user = (record.get("data") or {}).get("text")
        except OSError:
            continue
        info = {
            "path": str(path),
            "name": path.name,
            "ts": ts,
            "first_user_message": first_user,
            "cwd": meta_cwd,
        }
        if meta_cwd == target:
            return info
        if fallback is None:
            fallback = info
    return fallback


# ----------------------------------------------------------------------
# Utilitários
# ----------------------------------------------------------------------
def _now_iso() -> str:
    """Timestamp ISO-8601 com precisão de segundos."""
    return datetime.now().isoformat(timespec="seconds")


def _safe_data(data: Any) -> Any:
    """Garante que `data` é serializável em JSON; senão devolve repr."""
    try:
        json.dumps(data, ensure_ascii=False)
        return data
    except (TypeError, ValueError):
        if isinstance(data, dict):
            return {k: _safe_value(v) for k, v in data.items()}
        return {"_repr": str(data)}


def _safe_value(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return str(value)
