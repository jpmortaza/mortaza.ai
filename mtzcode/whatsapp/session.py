"""Gerenciador de sessões por contato do WhatsApp.

Cada JID mantém seu próprio `Agent` com histórico isolado e workspace próprio
(diretório `workspace/<digitos>` dentro do `MTZCODE_WHATSAPP_ROOT` ou cwd).

Sessões inativas por mais de `idle_ttl_s` são descartadas para liberar memória.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mtzcode.agent import Agent
from mtzcode.client import make_client
from mtzcode.config import Config
from mtzcode.tools.base import ToolRegistry
from mtzcode.whatsapp.evolution import _strip_jid


def _key_for(jid: str) -> str:
    """Chave de cache: só dígitos, ou 'anon' se vazio."""
    return _strip_jid(jid) or "anon"


def _env_truthy(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in (
        "1", "true", "yes", "sim", "on",
    )


@dataclass
class Session:
    jid: str
    agent: Agent
    client: Any
    workspace: Path
    last_seen: float = field(default_factory=time.time)
    # Lock por sessão — Agent.run() mexe em self.history e não é reentrante.
    lock: threading.Lock = field(default_factory=threading.Lock)

    def touch(self) -> None:
        self.last_seen = time.time()


class SessionManager:
    """Cache LRU-ish de sessões. Thread-safe."""

    def __init__(
        self,
        cfg: Config,
        registry: ToolRegistry,
        system_prompt: str,
        *,
        max_sessions: int = 50,
        idle_ttl_s: float = 3600.0,
        workspace_root: Path | None = None,
        auto_confirm: bool | None = None,
    ) -> None:
        self.cfg = cfg
        self.registry = registry
        self.system_prompt = system_prompt
        self.max_sessions = max_sessions
        self.idle_ttl_s = idle_ttl_s
        self.workspace_root = workspace_root or Path(
            os.environ.get("MTZCODE_WHATSAPP_ROOT", "./workspace")
        ).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        # Auto-confirm de tools destrutivas. False = denied (default seguro).
        if auto_confirm is None:
            auto_confirm = _env_truthy("WHATSAPP_AUTO_CONFIRM")
        self.auto_confirm = bool(auto_confirm)
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def _confirm_cb(self, _name: str, _args: dict[str, Any]) -> bool:
        return self.auto_confirm

    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def get(self, jid: str) -> Session:
        key = _key_for(jid)
        with self._lock:
            sess = self._sessions.get(key)
            now = time.time()
            if sess and (now - sess.last_seen) <= self.idle_ttl_s:
                sess.touch()
                return sess
            if sess:
                self._drop(key)
            if len(self._sessions) >= self.max_sessions:
                # Evict o mais antigo. NÃO fechamos o httpx client porque
                # outro thread pode estar com request in-flight; GC fecha
                # quando refcount = 0.
                oldest = min(
                    self._sessions, key=lambda k: self._sessions[k].last_seen
                )
                self._drop(oldest)

            workspace = self.workspace_root / key
            workspace.mkdir(parents=True, exist_ok=True)
            client = make_client(self.cfg.profile, self.cfg.request_timeout_s)
            agent = Agent(
                client=client,
                registry=self.registry,
                system_prompt=self.system_prompt,
                confirm_cb=self._confirm_cb,
                runtime_label=f"wa:{key}",
            )
            sess = Session(jid=jid, agent=agent, client=client, workspace=workspace)
            self._sessions[key] = sess
            return sess

    def reset(self, jid: str) -> None:
        key = _key_for(jid)
        with self._lock:
            self._drop(key)

    def _drop(self, key: str) -> None:
        """Remove a sessão do cache. NÃO fecha o httpx client — pode haver
        request in-flight; deixa o GC fechar quando refs caírem a zero.
        """
        self._sessions.pop(key, None)

    def shutdown(self) -> None:
        with self._lock:
            # No shutdown geral é seguro fechar — não há mais requests novos.
            for sess in self._sessions.values():
                try:
                    sess.client.close()
                except Exception:
                    pass
            self._sessions.clear()
