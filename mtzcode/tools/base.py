"""Base classes para tools do mtzcode.

Cada tool é uma subclasse de `Tool` que declara:
  - name: identificador único usado pelo modelo
  - description: texto que vai pro modelo explicando quando usar
  - Args: classe Pydantic com os parâmetros de entrada
  - run(args): executa e retorna uma string (resultado pro modelo)

A classe `ToolRegistry` mantém um mapa nome → instância e gera os schemas
no formato OpenAI/Ollama (`{type:'function', function:{...}}`).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError


class ToolError(RuntimeError):
    """Erro recuperável ao executar uma tool. A mensagem volta pro modelo."""


def _slim_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove ruído de um JSONSchema gerado pelo Pydantic.

    Tira ``title`` e ``description`` recursivos e descarta defs vazios.
    Preserva ``type``, ``properties``, ``required``, ``enum``, ``items``.
    """
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k in ("title", "description", "$defs", "definitions"):
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: _slim_schema(pv) for pk, pv in v.items()}
        elif isinstance(v, dict):
            out[k] = _slim_schema(v)
        elif isinstance(v, list):
            out[k] = [_slim_schema(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    return out


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    Args: ClassVar[type[BaseModel]]
    # Tools destrutivas disparam prompt de confirmação antes de executar.
    destructive: ClassVar[bool] = False
    # Cache de schemas (gerado preguiçosamente, imutável após a 1ª chamada).
    _schema_cache_full: ClassVar[dict[str, Any] | None] = None
    _schema_cache_slim: ClassVar[dict[str, Any] | None] = None

    def schema(self, slim: bool = False) -> dict[str, Any]:
        """Schema OpenAI/Ollama-style para tool calling.

        Em ``slim=True`` a descrição vira só a primeira linha (até 100 chars)
        e os campos do Args perdem ``title``/``description`` longas.
        Reduz drasticamente o overhead de tokens em modelos locais.
        """
        # Cache por instância (não por classe — uma instância por registry)
        cache_attr = "_schema_cache_slim" if slim else "_schema_cache_full"
        cached = getattr(self, cache_attr, None)
        if cached is not None:
            return cached
        if slim:
            desc = (self.description or "").strip().split("\n")[0]
            if len(desc) > 100:
                desc = desc[:97] + "..."
            params = _slim_schema(self.Args.model_json_schema())
        else:
            desc = self.description
            params = self.Args.model_json_schema()
        built = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": desc,
                "parameters": params,
            },
        }
        try:
            object.__setattr__(self, cache_attr, built)
        except (AttributeError, TypeError):
            pass
        return built

    def call(self, raw_args: dict[str, Any]) -> str:
        """Valida os argumentos e despacha para `run()`. Retorna sempre string."""
        try:
            args = self.Args.model_validate(raw_args or {})
        except ValidationError as exc:
            raise ToolError(f"argumentos inválidos para `{self.name}`: {exc}") from exc
        try:
            result = self.run(args)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 — devolve qualquer erro pro modelo
            raise ToolError(f"erro em `{self.name}`: {exc}") from exc
        if not isinstance(result, str):
            result = str(result)
        return result

    @abstractmethod
    def run(self, args: BaseModel) -> str:
        """Implementação concreta. Recebe args validados, retorna string."""


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool duplicada: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolError(f"tool desconhecida: `{name}`")
        return self._tools[name]

    def schemas(self, slim: bool = False) -> list[dict[str, Any]]:
        return [t.schema(slim=slim) for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)
