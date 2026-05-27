"""Servidor FastAPI do mtzcode.

Expõe uma UI web local e endpoints JSON/SSE que envolvem o mesmo Agent/Tools
usados pelo CLI. Pensado pra rodar só em localhost.

Endpoints originais:
  GET  /             — HTML da UI
  GET  /api/state    — estado atual (profile, cwd, versão, auto_mode, ...)
  GET  /api/profiles — lista de perfis disponíveis
  POST /api/profile  — troca o profile ativo { name: "qwen-7b" }
  POST /api/cwd      — troca o diretório de trabalho { path: "/abs/path" }
  POST /api/tree     — lista arquivos do cwd atual
  POST /api/chat     — SSE: envia { message } e recebe stream de eventos
  POST /api/reset    — limpa o histórico do agent

Endpoints estendidos (skills/sessions/folders/auto/schedules/history) — ver
funções decoradas mais abaixo.
"""
from __future__ import annotations

import json
import os
import queue
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from mtzcode import __version__
from mtzcode.agent import Agent, AgentEvent
from mtzcode.client import ChatClient, ChatClientError, make_client
from mtzcode.config import Config
from mtzcode.profiles import PROFILES, Profile, get_profile, list_profiles
from mtzcode.tools import default_registry

# ---------------------------------------------------------------------------
# Imports lazy/opcionais — módulos novos podem não existir em todos os envs.
# Se faltarem, os endpoints que dependem deles devolvem 503.
# ---------------------------------------------------------------------------
try:
    from mtzcode.session_log import (  # type: ignore
        SessionLogger,
        list_sessions as _list_sessions,
        load_session as _load_session,
        make_event_callback,
    )
    _SESSION_LOG_AVAILABLE = True
except ImportError:  # pragma: no cover
    SessionLogger = None  # type: ignore
    _list_sessions = None  # type: ignore
    _load_session = None  # type: ignore
    make_event_callback = None  # type: ignore
    _SESSION_LOG_AVAILABLE = False

try:
    from mtzcode.mcp import MCPManager, MCPServerConfig  # type: ignore  # noqa: F401
    _MCP_AVAILABLE = True
except ImportError:  # pragma: no cover
    MCPManager = None  # type: ignore
    MCPServerConfig = None  # type: ignore
    _MCP_AVAILABLE = False

try:
    from mtzcode.scheduler.cli_commands import (  # type: ignore
        add_task as _sched_add,
        list_tasks as _sched_list,
        remove_task as _sched_remove,
        run_task_now as _sched_run,
    )
    _SCHEDULER_AVAILABLE = True
except ImportError:  # pragma: no cover
    _sched_add = None  # type: ignore
    _sched_list = None  # type: ignore
    _sched_remove = None  # type: ignore
    _sched_run = None  # type: ignore
    _SCHEDULER_AVAILABLE = False

try:
    from mtzcode.autonomous import auto_confirm_factory  # type: ignore
    _AUTO_AVAILABLE = True
except ImportError:  # pragma: no cover
    auto_confirm_factory = None  # type: ignore
    _AUTO_AVAILABLE = False

try:
    from mtzcode.commands import load_commands as _load_slash_commands  # type: ignore
except ImportError:  # pragma: no cover
    _load_slash_commands = None  # type: ignore


# Caminhos de configuração persistente.
_MTZ_HOME = Path.home() / ".mtzcode"
_MCP_CONFIG_PATH = _MTZ_HOME / "mcp_servers.json"
_FOLDERS_PATH = _MTZ_HOME / "folders.json"


# ---------------------------------------------------------------------------
# Session — estado do servidor (processo single-user, sem auth, só localhost)
# ---------------------------------------------------------------------------
class Session:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.cwd: Path = Path.cwd()
        self.client = make_client(cfg.profile, cfg.request_timeout_s)
        self.registry = default_registry()
        # Web UI auto-approva tools destrutivas (usuário confirma pela UI depois)
        self.agent = Agent(
            self.client,
            self.registry,
            cfg.system_prompt(),
            confirm_cb=None,  # sem confirmação — UI pode exigir depois se quiser
        )
        # Lock garante que requisições concorrentes de /api/chat não interleave
        # o histórico do mesmo agent.
        self.chat_lock = threading.Lock()

        # ------------------------------------------------------------------
        # Estado estendido da UI: skills/tools desabilitadas, auto mode,
        # session logger e monkey-patch do registry.schemas() pra filtrar.
        # ------------------------------------------------------------------
        self.disabled_tools: set[str] = set()
        self.auto_mode: bool = False
        self.session_logger: Any = None  # SessionLogger | None

        # Monkey-patch: substitui registry.schemas() por uma closure que
        # filtra tools desabilitadas. Mantém método original como fallback.
        self._original_schemas = self.registry.schemas
        self.registry.schemas = self._filtered_schemas  # type: ignore[method-assign]

        # Filtro adicional no dispatch: tool desabilitada não executa
        # nem mesmo se o modelo chamar (o schema já omite, mas garantimos).
        self._original_get = self.registry.get

        def _filtered_get(name: str):
            if name in self.disabled_tools:
                from mtzcode.tools.base import ToolError as _TE
                raise _TE(
                    f"habilidade `{name}` está desabilitada nesta sessão."
                )
            return self._original_get(name)

        self.registry.get = _filtered_get  # type: ignore[method-assign]

        # Inicia logger de sessão (silenciosamente — falha não quebra o web).
        if _SESSION_LOG_AVAILABLE and SessionLogger is not None:
            try:
                self.session_logger = SessionLogger()
            except Exception:  # noqa: BLE001
                self.session_logger = None

    # ----- skills / tools desabilitadas ------------------------------------
    def _filtered_schemas(self, slim: bool = False) -> list[dict[str, Any]]:
        """Retorna schemas omitindo tools listadas em ``disabled_tools``."""
        try:
            all_schemas = self._original_schemas(slim=slim)  # type: ignore[call-arg]
        except TypeError:
            all_schemas = self._original_schemas()
        if not self.disabled_tools:
            return all_schemas
        return [
            s
            for s in all_schemas
            if s.get("function", {}).get("name") not in self.disabled_tools
        ]

    def disable_tool(self, name: str) -> None:
        self.disabled_tools.add(name)

    def enable_tool(self, name: str) -> None:
        self.disabled_tools.discard(name)

    # ----- profile / cwd ---------------------------------------------------
    def switch_profile(self, profile: Profile) -> None:
        new_client = make_client(profile, self.cfg.request_timeout_s)
        self.client.close()
        self.client = new_client
        self.agent.client = new_client
        self.cfg = self.cfg.with_profile(profile)

    def set_cwd(self, path: Path) -> None:
        path = path.expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise ValueError(f"diretório inválido: {path}")
        self.cwd = path
        os.chdir(path)  # tools usam cwd do processo

    def reset(self) -> None:
        self.agent.reset()

    # ----- auto mode -------------------------------------------------------
    def set_auto_mode(self, enabled: bool) -> None:
        """Liga/desliga o auto mode instalando o ``auto_confirm_factory``."""
        self.auto_mode = bool(enabled)
        if self.auto_mode and _AUTO_AVAILABLE and auto_confirm_factory is not None:
            cb = auto_confirm_factory()
        else:
            cb = None
        self.agent.confirm_cb = cb

    # ----- session logger --------------------------------------------------
    def rotate_session_logger(self) -> None:
        """Fecha o logger atual e abre um novo. Reset do agent fica a cargo
        do chamador (geralmente em /api/sessions/new).
        """
        old = self.session_logger
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        self.session_logger = None
        if _SESSION_LOG_AVAILABLE and SessionLogger is not None:
            try:
                self.session_logger = SessionLogger()
            except Exception:  # noqa: BLE001
                self.session_logger = None

    def session_id(self) -> str | None:
        if self.session_logger is None:
            return None
        try:
            return Path(self.session_logger.path).name
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------
class ProfileRequest(BaseModel):
    name: str


class CwdRequest(BaseModel):
    path: str


class ChatRequest(BaseModel):
    message: str


class ToggleRequest(BaseModel):
    enabled: bool


class InstallMcpRequest(BaseModel):
    name: str
    command: str
    args: list[str] = []
    env: dict[str, str] = {}


class FolderRequest(BaseModel):
    path: str
    label: str | None = None


class FolderUseRequest(BaseModel):
    path: str


class FolderDeleteRequest(BaseModel):
    path: str


class ScheduleRequest(BaseModel):
    name: str
    cron: str
    prompt: str
    profile: str | None = None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app() -> FastAPI:
    app = FastAPI(title="mtzcode", version=__version__)
    cfg = Config.load()
    session = Session(cfg)

    index_html_path = Path(__file__).parent / "static" / "index.html"

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        try:
            return index_html_path.read_text(encoding="utf-8")
        except OSError:
            return "<h1>mtzcode</h1><p>index.html não encontrado.</p>"

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        p = session.cfg.profile
        return {
            "version": __version__,
            "cwd": str(session.cwd),
            "profile": {
                "name": p.name,
                "label": p.label,
                "is_local": p.is_local,
                "model": p.model,
            },
            "auto_mode": session.auto_mode,
            "session_id": session.session_id(),
            "disabled_tools": sorted(session.disabled_tools),
        }

    @app.get("/api/profiles")
    def profiles_list() -> dict[str, Any]:
        return {
            "profiles": [
                {
                    "name": p.name,
                    "label": p.label,
                    "is_local": p.is_local,
                    "model": p.model,
                    "description": p.description,
                }
                for p in list_profiles()
            ]
        }

    @app.post("/api/profile")
    def switch_profile_endpoint(req: ProfileRequest) -> dict[str, Any]:
        try:
            profile = get_profile(req.name)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            session.switch_profile(profile)
        except ChatClientError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "profile": profile.name}

    @app.post("/api/cwd")
    def set_cwd_endpoint(req: CwdRequest) -> dict[str, Any]:
        try:
            session.set_cwd(Path(req.path))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "cwd": str(session.cwd)}

    @app.post("/api/reset")
    def reset_endpoint() -> dict[str, Any]:
        session.reset()
        return {"ok": True}

    @app.get("/api/settings")
    def settings_get() -> dict[str, Any]:
        from mtzcode.settings import get_settings
        return get_settings().to_dict()

    @app.post("/api/settings")
    def settings_set(req: dict[str, Any]) -> dict[str, Any]:
        """Atualiza settings persistentes e aplica em tempo real."""
        from mtzcode.settings import get_settings, reload_settings
        s = get_settings()
        s.update_from_dict(req or {})
        s.save()
        reload_settings()
        # Reset do client é necessário se a key/perfil mudar — mas
        # opções de modelo (num_ctx etc) são lidas no próximo /chat,
        # então não precisa nada além de save+reload.
        return {"ok": True, "settings": get_settings().to_dict()}

    @app.get("/api/help")
    def help_endpoint() -> dict[str, Any]:
        """Conteúdo da seção de Ajuda — texto markdown estático."""
        return {
            "sections": [
                {
                    "title": "Como funciona",
                    "body": (
                        "O mtzcode é um assistente de código que roda 100% local "
                        "no seu Mac via Ollama. Tudo o que você digitar fica na "
                        "sua máquina — não vai pra cloud (a menos que você "
                        "selecione um perfil cloud como Groq).\n\n"
                        "Ele usa **tool calling**: quando você pede pra criar/ler/"
                        "editar arquivos, ele invoca habilidades reais (read, "
                        "write, edit, bash, find_files, etc) em vez de só gerar "
                        "texto."
                    ),
                },
                {
                    "title": "Configurações",
                    "body": (
                        "Acesse pelo botão ⚙️ no topo direito.\n\n"
                        "- **API Keys**: pra usar perfis cloud (Groq, Maritaca) "
                        "cole a key aqui — fica salva em "
                        "`~/.mtzcode/settings.json` e vira env var no startup.\n"
                        "- **Temperatura**: 0.0–1.0. Mais baixo = mais "
                        "determinístico (bom pra código). Default 0.3.\n"
                        "- **num_ctx**: tamanho da janela de contexto do Ollama. "
                        "Aumente se você tem muito histórico (default 16384).\n"
                        "- **keep_alive**: por quanto tempo o modelo fica "
                        "carregado em VRAM (default 30m). Aumente pra evitar "
                        "cold start.\n"
                        "- **Pasta de dados**: onde ficam knowledge base, "
                        "datasets de fine-tune e conversas salvas.\n"
                        "- **Contexto pessoal**: texto livre que vai como bloco "
                        "extra no system prompt. Use pra dar contexto sobre "
                        "você (nome, empresa, preferências de estilo, "
                        "convenções do projeto)."
                    ),
                },
                {
                    "title": "Como treinar em português (fine-tuning)",
                    "body": (
                        "**O que é fine-tuning?** Pegar um modelo open-source "
                        "(ex: Qwen 2.5 14B) e re-treinar ele com EXEMPLOS seus "
                        "(perguntas em PT-BR + respostas como você quer). O "
                        "modelo resultante fica enviesado pro seu estilo.\n\n"
                        "**O que precisa?**\n"
                        "1. Um Mac com Apple Silicon (M1/M2/M3/M4) — usa "
                        "`mlx-lm` que aproveita a Neural Engine.\n"
                        "2. Um dataset de exemplos. Mínimo viável: ~200 pares "
                        "pergunta/resposta. Bom: 1000+. Formato JSONL com "
                        "`{\"prompt\": \"...\", \"completion\": \"...\"}`.\n"
                        "3. Tempo: ~30min–2h numa M-series dependendo do "
                        "tamanho do dataset.\n\n"
                        "**Passo a passo:**\n"
                        "1. `pip install mlx-lm`\n"
                        "2. Coloca seus exemplos em "
                        "`~/.mtzcode/data/train.jsonl`\n"
                        "3. Roda `mtzcode finetune` — o comando faz LoRA "
                        "(adapter pequeno, não re-treina o modelo todo).\n"
                        "4. O resultado vira um perfil `mtzcode-pt` que você "
                        "seleciona no topo da UI.\n\n"
                        "**Dica:** comece SEM fine-tuning. Use o campo "
                        "\"Contexto pessoal\" pra dar instruções — geralmente "
                        "isso já resolve 80% dos casos. Fine-tune só se você "
                        "tem um caso muito específico (jargão da empresa, "
                        "código numa linguagem rara, estilo MUITO particular)."
                    ),
                },
                {
                    "title": "Habilidades (tools)",
                    "body": (
                        "O modelo tem ~27 habilidades. Principais:\n\n"
                        "- `read`/`write`/`edit` — manipular arquivos\n"
                        "- `glob`/`grep`/`search_code` — buscar no projeto\n"
                        "- `bash` — rodar comandos\n"
                        "- `find_files`/`find_images` — buscar em qualquer "
                        "lugar do Mac via Spotlight (super poderes!)\n"
                        "- `web_fetch`/`web_search` — internet\n"
                        "- `apify_run_actor`/`apify_list_actors` — scraping "
                        "via Apify (Google Maps, Instagram, Amazon, etc)\n"
                        "- `applescript`/`open_app`/`screenshot` — controle "
                        "do macOS\n"
                        "- `docx_read`/`pdf_read`/`xlsx_read` — documentos\n\n"
                        "Você pode desabilitar habilidades específicas no "
                        "painel direito (aba HABILIDADES)."
                    ),
                },
                {
                    "title": "Apify (scraping/automação)",
                    "body": (
                        "**Apify** é uma plataforma de scraping com milhares "
                        "de actors prontos: Google Maps, Instagram, Amazon, "
                        "TikTok, LinkedIn, Twitter, etc.\n\n"
                        "**Setup:**\n"
                        "1. Crie conta em https://apify.com (tem free tier).\n"
                        "2. Pegue sua API key em "
                        "https://console.apify.com/account/integrations\n"
                        "3. Cole em **Configurações > API Keys > "
                        "APIFY_API_KEY**.\n\n"
                        "**Tools disponíveis:**\n"
                        "- `apify_list_actors` — descobre actors. Sem busca, "
                        "lista os seus; com `search='instagram'` busca na "
                        "store pública.\n"
                        "- `apify_run_actor` — roda um actor passando o "
                        "input JSON dele e devolve o dataset.\n"
                        "- `apify_get_dataset` — relê itens de uma run "
                        "anterior por dataset_id.\n\n"
                        "**Exemplo de uso:** \"Use o Apify pra pegar as 5 "
                        "primeiras pizzarias de SP no Google Maps\" → o "
                        "modelo chama `apify_run_actor` com "
                        "`actor_id='compass/crawler-google-places'` e "
                        "`input={\"searchStringsArray\": [\"pizzaria SP\"], "
                        "\"maxCrawledPlaces\": 5}`.\n\n"
                        "**Custos:** cada run consome créditos da sua conta "
                        "Apify (não da OpenAI/Ollama). Free tier dá ~5 USD "
                        "de créditos por mês."
                    ),
                },
                {
                    "title": "Modo Auto",
                    "body": (
                        "Quando ativado (botão ⚡auto no topo), confirmações "
                        "destrutivas são automáticas. Use quando quiser que o "
                        "modelo trabalhe sem te interromper a cada `write` ou "
                        "`bash`. **Cuidado**: ele pode apagar arquivos sem "
                        "perguntar."
                    ),
                },
            ]
        }

    # ==================================================================
    # TRAINING — fine-tuning LoRA via mlx-lm
    # ==================================================================
    @app.get("/api/training/status")
    def training_status() -> dict[str, Any]:
        from mtzcode import training as _t
        return _t.status()

    @app.get("/api/training/datasets")
    def training_datasets() -> dict[str, Any]:
        from mtzcode import training as _t
        return {"datasets": _t.list_datasets()}

    @app.post("/api/training/upload")
    async def training_upload(file: UploadFile = File(...)) -> dict[str, Any]:
        from mtzcode import training as _t
        # Valida extensão e tamanho ANTES de ler tudo na memória
        fname = (file.filename or "").lower()
        if not (fname.endswith(".jsonl") or fname.endswith(".json") or fname.endswith(".txt") or fname.endswith(".md")):
            raise HTTPException(status_code=400, detail="aceito apenas .jsonl/.json/.txt/.md")
        max_bytes = 500 * 1024 * 1024  # 500MB
        if getattr(file, "size", None) and file.size and file.size > max_bytes:
            raise HTTPException(status_code=413, detail="arquivo grande demais (>500MB)")
        try:
            content = await file.read()
            if len(content) > max_bytes:
                raise HTTPException(status_code=413, detail="arquivo grande demais (>500MB)")
            info = _t.save_dataset(file.filename or "unnamed.jsonl", content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "file": info}

    # ==================================================================
    # TODOs — lista de tarefas persistente (espelho da tool todo_write)
    # ==================================================================
    @app.get("/api/todos")
    def todos_get() -> dict[str, Any]:
        from mtzcode import todos as _t
        data = _t.load_todos()
        return {
            "todos": data.get("todos") or [],
            "updated_at": data.get("updated_at"),
            "summary": _t.summarize(data.get("todos") or []),
        }

    @app.post("/api/todos")
    def todos_set(req: dict[str, Any]) -> dict[str, Any]:
        from mtzcode import todos as _t
        items = req.get("todos") or []
        try:
            return _t.save_todos(items)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/todos")
    def todos_clear() -> dict[str, Any]:
        from mtzcode import todos as _t
        _t.clear_todos()
        return {"ok": True}

    # ==================================================================
    # Orquestrador — planos (fase 1: Planner)
    # ==================================================================
    @app.get("/api/plans")
    def plans_list() -> dict[str, Any]:
        from mtzcode import orchestrator as _orch
        cur = _orch.current_plan()
        return {
            "plans": _orch.list_plans(),
            "current_id": (cur or {}).get("id"),
        }

    @app.get("/api/plans/current")
    def plans_current() -> dict[str, Any]:
        from mtzcode import orchestrator as _orch
        from mtzcode.orchestrator.store import summarize_plan
        plan = _orch.current_plan()
        if not plan:
            return {"plan": None}
        return {"plan": plan, "summary": summarize_plan(plan)}

    @app.get("/api/plans/{plan_id}")
    def plans_get(plan_id: str) -> dict[str, Any]:
        from mtzcode import orchestrator as _orch
        from mtzcode.orchestrator.store import summarize_plan
        plan = _orch.load_plan(plan_id)
        if not plan:
            raise HTTPException(status_code=404, detail="plano não encontrado")
        return {"plan": plan, "summary": summarize_plan(plan)}

    @app.post("/api/plans/{plan_id}/tasks/{task_id}")
    def plans_set_task(plan_id: str, task_id: str, req: dict[str, Any]) -> dict[str, Any]:
        from mtzcode import orchestrator as _orch
        from mtzcode.orchestrator.store import summarize_plan
        status = req.get("status")
        if not status:
            raise HTTPException(status_code=400, detail="status é obrigatório")
        try:
            plan = _orch.set_task_status(plan_id, task_id, status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"plan": plan, "summary": summarize_plan(plan)}

    @app.post("/api/plans/advance")
    def plans_advance() -> dict[str, Any]:
        from mtzcode import orchestrator as _orch
        from mtzcode.orchestrator.store import summarize_plan
        plan = _orch.advance_current()
        if not plan:
            raise HTTPException(status_code=404, detail="sem plano corrente")
        return {"plan": plan, "summary": summarize_plan(plan)}

    @app.post("/api/chat/attach")
    async def chat_attach(file: UploadFile = File(...)) -> dict[str, Any]:
        """Recebe um arquivo do chat e salva em ~/.mtzcode/uploads/.

        O frontend chama isso quando o usuário arrasta/anexa um arquivo no
        composer. Retorna o caminho absoluto pra que a próxima mensagem
        possa referenciar (o modelo então usa `read` pra acessar).
        """
        uploads_dir = Path.home() / ".mtzcode" / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        # Sanitiza o nome e prefixa com timestamp pra evitar colisão
        safe = Path(file.filename or "anexo.bin").name
        if not safe or safe.startswith("."):
            safe = "anexo.bin"
        # Valida tamanho declarado ANTES de ler (evita DoS por upload gigante)
        max_bytes = 50 * 1024 * 1024
        if getattr(file, "size", None) and file.size and file.size > max_bytes:
            raise HTTPException(status_code=413, detail="arquivo grande demais (>50MB)")
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = uploads_dir / f"{ts}-{safe}"
        try:
            content = await file.read()
            if len(content) > max_bytes:
                raise HTTPException(status_code=413, detail="arquivo grande demais (>50MB)")
            target.write_bytes(content)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"falha ao salvar: {exc}") from exc
        return {
            "ok": True,
            "name": safe,
            "path": str(target),
            "size": len(content),
        }

    @app.post("/api/training/format")
    def training_format() -> dict[str, Any]:
        from mtzcode import training as _t
        try:
            return _t.format_datasets()
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/training/datasets/{filename}")
    def training_delete_dataset(filename: str) -> dict[str, Any]:
        from mtzcode import training as _t
        ok = _t.delete_dataset(filename)
        if not ok:
            raise HTTPException(status_code=404, detail="dataset não encontrado")
        return {"ok": True}

    @app.get("/api/training/adapters")
    def training_adapters() -> dict[str, Any]:
        from mtzcode import training as _t
        return {"adapters": _t.list_adapters()}

    @app.post("/api/training/start")
    def training_start(req: dict[str, Any] | None = None) -> dict[str, Any]:
        from mtzcode import training as _t
        req = req or {}
        try:
            return _t.start_training(
                model=str(req.get("model") or "Qwen/Qwen2.5-14B-Instruct"),
                iters=int(req.get("iters") or 500),
                batch_size=int(req.get("batch_size") or 2),
                lora_layers=int(req.get("lora_layers") or 16),
                learning_rate=float(req.get("learning_rate") or 1e-5),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/training/stop")
    def training_stop() -> dict[str, Any]:
        from mtzcode import training as _t
        return _t.stop_training()

    @app.get("/api/training/logs")
    def training_logs(lines: int = 200) -> dict[str, Any]:
        from mtzcode import training as _t
        # Clamp pra evitar OOM se cliente pedir milhões de linhas
        lines = max(1, min(int(lines or 200), 5000))
        return {"log": _t.tail_log(max_lines=lines), "job": _t.get_job().to_dict()}

    @app.get("/api/browse")
    def browse_endpoint(path: str | None = None) -> dict[str, Any]:
        """Lista subdiretórios de um path. Se vazio, começa em $HOME.

        Usado pelo modal "trocar pasta" da UI pra navegar visualmente em
        vez de exigir o caminho absoluto digitado.
        """
        base = Path(path).expanduser() if path else Path.home()
        try:
            base = base.resolve()
        except OSError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not base.exists() or not base.is_dir():
            raise HTTPException(status_code=404, detail=f"diretório não encontrado: {base}")
        entries: list[dict[str, Any]] = []
        try:
            for p in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if p.name.startswith("."):
                    continue
                if not p.is_dir():
                    continue
                entries.append({"name": p.name, "path": str(p)})
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        parent = str(base.parent) if base.parent != base else None
        return {
            "path": str(base),
            "parent": parent,
            "home": str(Path.home()),
            "entries": entries,
        }

    @app.post("/api/tree")
    def tree_endpoint() -> dict[str, Any]:
        """Lista arquivos do cwd (até ~500 entradas, respeitando excludes comuns)."""
        excludes = {
            ".git", ".venv", "venv", "__pycache__", "node_modules",
            "dist", "build", ".pytest_cache", ".ruff_cache", ".DS_Store",
        }
        entries: list[dict[str, Any]] = []
        count = 0
        for p in sorted(session.cwd.rglob("*")):
            if count >= 500:
                break
            if any(part in excludes for part in p.parts):
                continue
            rel = p.relative_to(session.cwd)
            entries.append(
                {
                    "path": str(rel),
                    "is_dir": p.is_dir(),
                    "depth": len(rel.parts),
                }
            )
            count += 1
        return {"root": str(session.cwd), "entries": entries, "truncated": count >= 500}

    @app.post("/api/chat")
    def chat_endpoint(req: ChatRequest) -> StreamingResponse:
        """Envia a mensagem pro agent e devolve eventos SSE token-a-token.

        Agent roda em thread separada; o gerador consome uma queue de eventos
        e dá yield imediato, permitindo streaming real.

        Kinds: text_delta | tool_call | tool_result | tool_error |
               tool_denied | assistant_text_end | done | error
        """
        # Sentinela pra sinalizar fim do stream
        _DONE = object()
        q: queue.Queue = queue.Queue()

        def serialize(kind: str, data: dict) -> str:
            return json.dumps({"kind": kind, "data": data})

        def queue_cb(ev: AgentEvent) -> None:
            q.put(("event", serialize(ev.kind, ev.data)))

        # Encadeia: logger primeiro (persiste), depois queue_cb (UI).
        if (
            _SESSION_LOG_AVAILABLE
            and session.session_logger is not None
            and make_event_callback is not None
        ):
            on_event = make_event_callback(session.session_logger, queue_cb)
        else:
            on_event = queue_cb

        # Loga input do usuário pro arquivo de sessão também.
        if session.session_logger is not None:
            try:
                session.session_logger.log_user(req.message)
            except Exception:  # noqa: BLE001
                pass

        def worker() -> None:
            """Roda o agent na thread. Adquire lock pra não interleave com
            outras requisições simultâneas que compartilham o mesmo Session.
            """
            with session.chat_lock:
                try:
                    final = session.agent.run_streaming(
                        req.message, on_event=on_event
                    )
                    q.put(("event", serialize("done", {"text": final})))
                except ChatClientError as exc:
                    q.put(("event", serialize("error", {"message": str(exc)})))
                except Exception as exc:  # noqa: BLE001
                    q.put(("event", serialize("error", {"message": str(exc)})))
                finally:
                    q.put(("done", _DONE))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        def event_stream():
            while True:
                try:
                    kind, payload = q.get(timeout=600)
                except queue.Empty:
                    yield "data: {\"kind\":\"error\",\"data\":{\"message\":\"timeout\"}}\n\n"
                    return
                if kind == "done":
                    return
                yield f"data: {payload}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ==================================================================
    # SKILLS — catálogo unificado de tools nativas + MCP servers + slash
    # ==================================================================
    @app.get("/api/skills")
    def skills_list() -> dict[str, Any]:
        """Lista combinada de skills (tools nativas, MCP servers, slash commands)."""
        skills: list[dict[str, Any]] = []

        # 1) Tools nativas — vêm do registry original (não filtrado).
        for schema in session._original_schemas():
            fn = schema.get("function", {}) or {}
            name = fn.get("name", "")
            try:
                tool = session.registry.get(name)
                destructive = bool(getattr(tool, "destructive", False))
            except Exception:  # noqa: BLE001
                destructive = False
            skills.append(
                {
                    "id": f"tool:{name}",
                    "kind": "tool",
                    "name": name,
                    "description": fn.get("description", ""),
                    "enabled": name not in session.disabled_tools,
                    "destructive": destructive,
                }
            )

        # 2) MCP servers — lê o JSON cru pra preservar o estado enabled/disabled.
        for entry in _read_mcp_servers():
            skills.append(
                {
                    "id": f"mcp:{entry['name']}",
                    "kind": "mcp_server",
                    "name": entry["name"],
                    "description": entry.get("description")
                    or f"Servidor MCP {entry['name']}",
                    "enabled": entry.get("enabled", True),
                    "command": entry.get("command", ""),
                    "args": entry.get("args", []),
                }
            )

        # 3) Slash commands customizados.
        if _load_slash_commands is not None:
            try:
                cmds = _load_slash_commands()
            except Exception:  # noqa: BLE001
                cmds = {}
            for name, cmd in cmds.items():
                # Description = primeira linha não vazia do template.
                desc = ""
                for line in (cmd.template or "").splitlines():
                    line = line.strip()
                    if line:
                        desc = line[:200]
                        break
                skills.append(
                    {
                        "id": f"command:{name}",
                        "kind": "slash_command",
                        "name": name,
                        "description": desc,
                        "enabled": True,
                    }
                )

        return {"skills": skills}

    @app.post("/api/skills/{skill_id}/toggle")
    def skill_toggle(skill_id: str, req: ToggleRequest) -> dict[str, Any]:
        """Ativa/desativa uma skill. ``skill_id`` é ``kind:name``."""
        if ":" not in skill_id:
            raise HTTPException(status_code=400, detail="skill_id inválido")
        kind, _, name = skill_id.partition(":")

        if kind == "tool":
            if req.enabled:
                session.enable_tool(name)
            else:
                session.disable_tool(name)
            return {"ok": True, "id": skill_id, "enabled": req.enabled}

        if kind == "mcp":
            data = _read_mcp_raw()
            servers = data.setdefault("mcpServers", {})
            if name not in servers:
                raise HTTPException(status_code=404, detail=f"MCP server `{name}` não existe")
            servers[name]["enabled"] = bool(req.enabled)
            # Mantém compat com o campo `disabled` (Claude Desktop usa esse).
            servers[name]["disabled"] = not bool(req.enabled)
            _write_mcp_raw(data)
            return {"ok": True, "id": skill_id, "enabled": req.enabled}

        if kind == "command":
            # Slash commands são sempre habilitados — no-op.
            return {"ok": True, "id": skill_id, "enabled": True, "noop": True}

        raise HTTPException(status_code=400, detail=f"kind desconhecido: {kind}")

    @app.post("/api/skills/install_mcp")
    def skill_install_mcp(req: InstallMcpRequest) -> dict[str, Any]:
        """Instala (registra) um servidor MCP novo no ``mcp_servers.json``."""
        data = _read_mcp_raw()
        servers = data.setdefault("mcpServers", {})
        if req.name in servers:
            raise HTTPException(
                status_code=400, detail=f"servidor `{req.name}` já existe"
            )
        servers[req.name] = {
            "command": req.command,
            "args": list(req.args or []),
            "env": dict(req.env or {}),
            "enabled": True,
        }
        _write_mcp_raw(data)
        return {"ok": True, "name": req.name}

    @app.delete("/api/skills/install_mcp/{name}")
    def skill_uninstall_mcp(name: str) -> dict[str, Any]:
        data = _read_mcp_raw()
        servers = data.setdefault("mcpServers", {})
        if name not in servers:
            raise HTTPException(status_code=404, detail=f"servidor `{name}` não existe")
        del servers[name]
        _write_mcp_raw(data)
        return {"ok": True, "name": name}

    @app.get("/api/skills/marketplace")
    def skill_marketplace() -> dict[str, Any]:
        """Catálogo curado de servidores MCP populares pra instalação rápida."""
        return {"servers": _MCP_MARKETPLACE}

    # ==================================================================
    # SESSIONS — listagem, inspeção e resume de conversas anteriores
    # ==================================================================
    @app.get("/api/sessions")
    def sessions_list() -> dict[str, Any]:
        if not _SESSION_LOG_AVAILABLE or _list_sessions is None:
            raise HTTPException(status_code=503, detail="session_log indisponível")
        items = _list_sessions()
        out: list[dict[str, Any]] = []
        for s in items:
            path = s.get("path", "")
            sid = Path(path).name if path else ""
            out.append(
                {
                    "id": sid,
                    "path": path,
                    "ts": s.get("ts"),
                    "first_user_message": s.get("first_user_message"),
                }
            )
        return {"sessions": out}

    @app.get("/api/sessions/{session_id}")
    def sessions_get(session_id: str) -> dict[str, Any]:
        if not _SESSION_LOG_AVAILABLE or _load_session is None:
            raise HTTPException(status_code=503, detail="session_log indisponível")
        path = _session_path_from_id(session_id)
        if path is None or not path.exists():
            raise HTTPException(status_code=404, detail="sessão não encontrada")
        events = _load_session(path)
        return {"id": session_id, "events": events, "count": len(events)}

    @app.post("/api/sessions/{session_id}/resume")
    def sessions_resume(session_id: str) -> dict[str, Any]:
        """Reconstrói o ``agent.history`` a partir de uma sessão antiga."""
        if not _SESSION_LOG_AVAILABLE or _load_session is None:
            raise HTTPException(status_code=503, detail="session_log indisponível")
        path = _session_path_from_id(session_id)
        if path is None or not path.exists():
            raise HTTPException(status_code=404, detail="sessão não encontrada")
        events = _load_session(path)
        from mtzcode.session_log import events_to_history
        history = events_to_history(events)
        with session.chat_lock:
            session.agent.reset()
            session.agent.history.extend(history)
        return {"ok": True, "id": session_id, "messages": len(session.agent.history)}

    @app.delete("/api/sessions/{session_id}")
    def sessions_delete(session_id: str) -> dict[str, Any]:
        """Apaga o arquivo .jsonl de uma sessão."""
        path = _session_path_from_id(session_id)
        if path is None or not path.exists():
            raise HTTPException(status_code=404, detail="sessão não encontrada")
        try:
            path.unlink()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "id": session_id}

    @app.delete("/api/sessions")
    def sessions_delete_all() -> dict[str, Any]:
        """Apaga TODAS as sessões salvas. Cuidado."""
        if not _SESSION_LOG_AVAILABLE or _list_sessions is None:
            raise HTTPException(status_code=503, detail="session_log indisponível")
        items = _list_sessions()
        deleted = 0
        for s in items:
            p = Path(s.get("path", ""))
            if p.exists():
                try:
                    p.unlink()
                    deleted += 1
                except OSError:
                    pass
        return {"ok": True, "deleted": deleted}

    @app.post("/api/sessions/new")
    def sessions_new() -> dict[str, Any]:
        """Fecha o logger atual, abre outro e reseta o agent."""
        with session.chat_lock:
            session.rotate_session_logger()
            session.agent.reset()
        return {"ok": True, "session_id": session.session_id()}

    # ==================================================================
    # FOLDERS — pastas favoritas persistidas
    # ==================================================================
    @app.get("/api/folders")
    def folders_list() -> dict[str, Any]:
        folders = _read_folders()
        cur = str(session.cwd)
        # Marca o cwd atual; se não estiver na lista, injeta como primeiro.
        out: list[dict[str, Any]] = []
        seen_current = False
        for f in folders:
            entry = dict(f)
            entry["current"] = entry.get("path") == cur
            if entry["current"]:
                seen_current = True
            out.append(entry)
        if not seen_current:
            out.insert(
                0,
                {
                    "path": cur,
                    "label": Path(cur).name or cur,
                    "last_used": None,
                    "current": True,
                    "transient": True,
                },
            )
        return {"folders": out}

    @app.post("/api/folders")
    def folders_add(req: FolderRequest) -> dict[str, Any]:
        path = str(Path(req.path).expanduser().resolve())
        folders = _read_folders()
        for f in folders:
            if f.get("path") == path:
                # Já existe — só atualiza label se foi passado.
                if req.label:
                    f["label"] = req.label
                _write_folders(folders)
                return {"ok": True, "folder": f}
        entry = {
            "path": path,
            "label": req.label or Path(path).name or path,
            "last_used": None,
        }
        folders.append(entry)
        _write_folders(folders)
        return {"ok": True, "folder": entry}

    @app.delete("/api/folders")
    def folders_remove(req: FolderDeleteRequest) -> dict[str, Any]:
        path = str(Path(req.path).expanduser().resolve())
        folders = _read_folders()
        new_folders = [f for f in folders if f.get("path") != path]
        if len(new_folders) == len(folders):
            raise HTTPException(status_code=404, detail="folder não registrado")
        _write_folders(new_folders)
        return {"ok": True}

    @app.post("/api/folders/use")
    def folders_use(req: FolderUseRequest) -> dict[str, Any]:
        """Atalho: troca o cwd e atualiza ``last_used`` no folder se existir."""
        try:
            session.set_cwd(Path(req.path))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        folders = _read_folders()
        target = str(session.cwd)
        for f in folders:
            if f.get("path") == target:
                f["last_used"] = datetime.now().isoformat(timespec="seconds")
                break
        _write_folders(folders)
        return {"ok": True, "cwd": str(session.cwd)}

    # ==================================================================
    # AUTO MODE
    # ==================================================================
    @app.get("/api/auto")
    def auto_get() -> dict[str, Any]:
        return {"enabled": session.auto_mode}

    @app.post("/api/auto")
    def auto_set(req: ToggleRequest) -> dict[str, Any]:
        if req.enabled and not _AUTO_AVAILABLE:
            raise HTTPException(status_code=503, detail="autonomous indisponível")
        session.set_auto_mode(req.enabled)
        return {"ok": True, "enabled": session.auto_mode}

    # ==================================================================
    # SCHEDULES
    # ==================================================================
    @app.get("/api/schedules")
    def schedules_list() -> dict[str, Any]:
        if not _SCHEDULER_AVAILABLE or _sched_list is None:
            raise HTTPException(status_code=503, detail="scheduler indisponível")
        tasks = _sched_list()
        return {"schedules": [_serialize_task(t) for t in tasks]}

    @app.post("/api/schedules")
    def schedules_add(req: ScheduleRequest) -> dict[str, Any]:
        if not _SCHEDULER_AVAILABLE or _sched_add is None:
            raise HTTPException(status_code=503, detail="scheduler indisponível")
        try:
            tid = _sched_add(
                name=req.name,
                cron=req.cron,
                prompt=req.prompt,
                profile=req.profile,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "id": tid}

    @app.delete("/api/schedules/{task_id}")
    def schedules_remove(task_id: str) -> dict[str, Any]:
        if not _SCHEDULER_AVAILABLE or _sched_remove is None:
            raise HTTPException(status_code=503, detail="scheduler indisponível")
        ok = _sched_remove(task_id)
        if not ok:
            raise HTTPException(status_code=404, detail="task não encontrada")
        return {"ok": True}

    @app.post("/api/schedules/{task_id}/run")
    def schedules_run(task_id: str) -> dict[str, Any]:
        if not _SCHEDULER_AVAILABLE or _sched_run is None:
            raise HTTPException(status_code=503, detail="scheduler indisponível")
        success, summary = _sched_run(task_id)
        return {"ok": success, "summary": summary}

    # ==================================================================
    # HISTORY
    # ==================================================================
    @app.get("/api/history")
    def history_get(skip: int = 0, limit: int = 200) -> dict[str, Any]:
        """Retorna o histórico atual do agent paginado.

        Sem paginação, históricos longos (>5k msgs) travam a UI no GET.
        """
        skip = max(0, int(skip or 0))
        limit = max(1, min(int(limit or 200), 1000))
        full = session.agent.history
        total = len(full)
        slice_ = full[skip : skip + limit]
        out: list[dict[str, Any]] = []
        for msg in slice_:
            role = msg.get("role", "")
            content = msg.get("content", "") or ""
            if isinstance(content, str) and len(content) > 2000:
                content = content[:2000] + "\n…[truncado]"
            entry: dict[str, Any] = {"role": role, "content": content}
            tcs = msg.get("tool_calls")
            if tcs:
                entry["tool_calls"] = [
                    {
                        "name": (tc.get("function", {}) or {}).get("name", ""),
                        "id": tc.get("id"),
                    }
                    for tc in tcs
                ]
            if role == "tool":
                entry["name"] = msg.get("name")
                entry["tool_call_id"] = msg.get("tool_call_id")
            out.append(entry)
        return {
            "history": out,
            "count": len(out),
            "total": total,
            "skip": skip,
            "limit": limit,
        }

    # WhatsApp / Evolution API — só monta se configurado.
    if os.environ.get("EVOLUTION_BASE_URL") and os.environ.get("EVOLUTION_API_KEY"):
        try:
            from mtzcode.whatsapp import SessionManager, setup_webhook
            # Registry separada da web Session — a Session do web monkey-patcha
            # .get/.schemas pra honrar a lista de tools desabilitadas via UI,
            # e esse estado não deve vazar pras conversas do WhatsApp.
            wa_registry = default_registry()
            wa_manager = SessionManager(
                cfg=session.cfg,
                registry=wa_registry,
                system_prompt=session.cfg.system_prompt(),
            )
            setup_webhook(app, wa_manager)
        except Exception as exc:  # noqa: BLE001
            # Não derruba o servidor se config WhatsApp falhar — só loga.
            import logging
            logging.getLogger("mtzcode.web").warning(
                "WhatsApp não montado: %s", exc
            )

    return app


# ---------------------------------------------------------------------------
# Helpers de IO — MCP servers, folders, sessions, scheduler serialization
# ---------------------------------------------------------------------------
def _read_mcp_raw() -> dict[str, Any]:
    """Lê o JSON bruto do mcp_servers.json (cria estrutura vazia se faltar)."""
    if not _MCP_CONFIG_PATH.exists():
        return {"mcpServers": {}}
    try:
        data = json.loads(_MCP_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"mcpServers": {}}
    if not isinstance(data, dict):
        return {"mcpServers": {}}
    if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
        data["mcpServers"] = {}
    return data


def _write_mcp_raw(data: dict[str, Any]) -> None:
    _MTZ_HOME.mkdir(parents=True, exist_ok=True)
    _MCP_CONFIG_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _read_mcp_servers() -> list[dict[str, Any]]:
    """Versão amigável: lista achatada com name + flags."""
    data = _read_mcp_raw()
    out: list[dict[str, Any]] = []
    for name, spec in (data.get("mcpServers") or {}).items():
        if not isinstance(spec, dict):
            continue
        # Compatibilidade: Claude Desktop usa `disabled`, nós usamos `enabled`.
        if "enabled" in spec:
            enabled = bool(spec.get("enabled", True))
        elif "disabled" in spec:
            enabled = not bool(spec.get("disabled", False))
        else:
            enabled = True
        out.append(
            {
                "name": name,
                "command": spec.get("command", ""),
                "args": spec.get("args", []) or [],
                "env": spec.get("env", {}) or {},
                "enabled": enabled,
                "description": spec.get("description"),
            }
        )
    return out


def _read_folders() -> list[dict[str, Any]]:
    if not _FOLDERS_PATH.exists():
        return []
    try:
        data = json.loads(_FOLDERS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [f for f in data if isinstance(f, dict) and "path" in f]


def _write_folders(folders: list[dict[str, Any]]) -> None:
    _MTZ_HOME.mkdir(parents=True, exist_ok=True)
    _FOLDERS_PATH.write_text(
        json.dumps(folders, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _session_path_from_id(session_id: str) -> Path | None:
    """Resolve um id (= filename) para o Path completo no log dir."""
    try:
        from mtzcode.session_log import DEFAULT_LOG_DIR  # type: ignore
    except ImportError:
        return None
    # Sanitiza pra evitar path traversal — usamos só basename.
    safe = Path(session_id).name
    return DEFAULT_LOG_DIR / safe


def _serialize_task(task: Any) -> dict[str, Any]:
    """Serializa um ScheduledTask (dataclass) ou dict pra JSON."""
    if is_dataclass(task):
        return asdict(task)
    if isinstance(task, dict):
        return task
    return {"repr": str(task)}


# ---------------------------------------------------------------------------
# Marketplace MCP — catálogo curado de servidores populares
# ---------------------------------------------------------------------------
_MCP_MARKETPLACE: list[dict[str, Any]] = [
    {
        "name": "filesystem",
        "description": "Acesso de leitura/escrita ao filesystem local",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", str(Path.home())],
    },
    {
        "name": "github",
        "description": "Repositórios, issues e PRs no GitHub",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
    },
    {
        "name": "brave-search",
        "description": "Busca web via Brave Search API",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-brave-search"],
    },
    {
        "name": "postgres",
        "description": "Conexão somente-leitura a um banco PostgreSQL",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-postgres"],
    },
    {
        "name": "sqlite",
        "description": "Consulta a banco SQLite local",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sqlite"],
    },
    {
        "name": "memory",
        "description": "Memória persistente baseada em knowledge graph",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
    },
    {
        "name": "sequential-thinking",
        "description": "Tool de raciocínio passo-a-passo estruturado",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
    },
    {
        "name": "puppeteer",
        "description": "Browser automation via Puppeteer",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-puppeteer"],
    },
]


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Entry point do `mtzcode serve`."""
    import uvicorn

    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="info")
