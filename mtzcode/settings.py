"""Configurações persistentes do mtzcode (editáveis pelo web UI).

Salva em ``~/.mtzcode/settings.json``. Carregadas no startup; mudanças
feitas via /api/settings são persistidas e aplicadas em tempo real
(API keys viram env vars, opções do modelo passam ao client, contexto
pessoal é concatenado ao system prompt, etc).

Tudo opcional — sem o arquivo, o mtzcode usa os defaults razoáveis.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SETTINGS_DIR = Path.home() / ".mtzcode"
SETTINGS_PATH = SETTINGS_DIR / "settings.json"


@dataclass
class ModelOptions:
    """Opções de inferência passadas ao backend (Ollama lê, cloud ignora)."""

    num_ctx: int = 16384
    num_predict: int = 2048
    temperature: float = 0.3
    top_p: float = 0.9
    keep_alive: str = "30m"

    def __post_init__(self) -> None:
        # Clamp em vez de raise: settings carregadas de disco podem estar
        # corrompidas e não queremos quebrar o startup. Logamos clamp implícito.
        if self.num_ctx < 4096:
            self.num_ctx = 4096
        if self.num_ctx > 131072:
            self.num_ctx = 131072
        if self.num_predict < 256:
            self.num_predict = 256
        if self.num_predict > 16384:
            self.num_predict = 16384
        if not (0.0 <= self.temperature <= 2.0):
            self.temperature = 0.3
        if not (0.0 < self.top_p <= 1.0):
            self.top_p = 0.9


@dataclass
class Settings:
    # API keys (vão pra env vars no apply())
    api_keys: dict[str, str] = field(default_factory=dict)
    # Opções de inferência
    model_options: ModelOptions = field(default_factory=ModelOptions)
    # Texto extra concatenado ao system prompt — instruções pessoais,
    # contexto da empresa, preferências de estilo, etc.
    personal_context: str = ""
    # Pasta padrão pra dados (knowledge base, datasets de fine-tune)
    data_folder: str = str(Path.home() / ".mtzcode" / "data")
    # Interpretador Python pra rodar mlx_lm.lora (override). Vazio = auto-detect.
    training_python: str = ""

    # ------------------------------------------------------------------
    # Carregamento / persistência
    # ------------------------------------------------------------------
    @classmethod
    def load(cls) -> "Settings":
        if not SETTINGS_PATH.exists():
            return cls()
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        opts_raw = data.get("model_options") or {}
        opts = ModelOptions(
            num_ctx=int(opts_raw.get("num_ctx", 16384)),
            num_predict=int(opts_raw.get("num_predict", 2048)),
            temperature=float(opts_raw.get("temperature", 0.3)),
            top_p=float(opts_raw.get("top_p", 0.9)),
            keep_alive=str(opts_raw.get("keep_alive", "30m")),
        )
        return cls(
            api_keys=dict(data.get("api_keys") or {}),
            model_options=opts,
            personal_context=str(data.get("personal_context") or ""),
            data_folder=str(
                data.get("data_folder") or (Path.home() / ".mtzcode" / "data")
            ),
            training_python=str(data.get("training_python") or ""),
        )

    def save(self) -> None:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        # asdict() lida com dataclasses aninhadas
        SETTINGS_PATH.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def apply(self) -> None:
        """Aplica settings ao processo: env vars de API keys e ModelOptions."""
        for key, value in self.api_keys.items():
            if value:
                os.environ[key] = value
        # Cria a pasta de dados se não existir
        try:
            Path(self.data_folder).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Mascara valores de api_keys pra não vazar pelo /api/settings GET
        d["api_keys"] = {k: ("***" if v else "") for k, v in d["api_keys"].items()}
        return d

    def update_from_dict(self, data: dict[str, Any]) -> None:
        if "api_keys" in data and isinstance(data["api_keys"], dict):
            for k, v in data["api_keys"].items():
                if v == "***":
                    # placeholder mascarado — preserva valor existente
                    continue
                self.api_keys[k] = str(v) if v is not None else ""
        if "model_options" in data and isinstance(data["model_options"], dict):
            mo = data["model_options"]
            try:
                if "num_ctx" in mo:
                    self.model_options.num_ctx = int(mo["num_ctx"])
                if "num_predict" in mo:
                    self.model_options.num_predict = int(mo["num_predict"])
                if "temperature" in mo:
                    self.model_options.temperature = float(mo["temperature"])
                if "top_p" in mo:
                    self.model_options.top_p = float(mo["top_p"])
                if "keep_alive" in mo:
                    self.model_options.keep_alive = str(mo["keep_alive"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"model_options inválidas: {exc}") from exc
            # Re-aplica clamps de __post_init__
            self.model_options.__post_init__()
        if "personal_context" in data:
            self.personal_context = str(data["personal_context"] or "")
        if "data_folder" in data:
            self.data_folder = str(data["data_folder"] or self.data_folder)
        if "training_python" in data:
            self.training_python = str(data["training_python"] or "")


# ----------------------------------------------------------------------
# Singleton — carregado uma vez no startup, modificado pelos endpoints
# ----------------------------------------------------------------------
_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.load()
        _settings.apply()
    return _settings


def reload_settings() -> Settings:
    """Recarrega do disco. Usado após /api/settings POST."""
    global _settings
    _settings = Settings.load()
    _settings.apply()
    return _settings
