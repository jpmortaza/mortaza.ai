"""Agent loop do mtzcode — conversa com o modelo, executa tools, repete
até o modelo terminar (sem mais tool_calls).

Suporta modo síncrono (`run`) e modo streaming (`run_streaming`, que emite
deltas de texto em tempo real para a UI).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from mtzcode import runtime as _runtime
from mtzcode.client import ChatClient, ChatClientError
from mtzcode.tools.base import Tool, ToolError, ToolRegistry

MAX_ITERATIONS = 25

# Quando o modelo emite texto que parece ser uma tool call mas o parser não
# consegue decodificar, devolvemos esta mensagem como "tool result" sintético
# pra forçar o modelo a retentar com o formato correto.
_RECOVERY_HINT = (
    "ERRO DE FORMATO: você escreveu o que parecia uma tool call em texto, "
    "mas o JSON estava inválido ou em formato errado. "
    "NÃO escreva JSON no texto da resposta. "
    "Use o mecanismo de tool calling do sistema (function calling). "
    "Retente AGORA chamando a tool corretamente — não desista, não peça desculpa, "
    "apenas faça a chamada certa."
)

# Regex para extrair blocos <tool_call>...</tool_call> (template Qwen oficial)
_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)

# Heurística: detecta texto que CHEIRA a tentativa de tool call (pra disparar
# auto-recuperação quando o parser falha em extrair).
_TOOL_CALL_SMELL_RE = re.compile(
    r'["\']\s*(?:name|function_name|tool|nome)\s*["\']\s*:\s*["\']',
    re.IGNORECASE,
)


@dataclass
class AgentEvent:
    """Evento emitido pelo agent durante execução, pra UI mostrar progresso."""
    kind: str  # text_delta | assistant_text | tool_call | tool_result | tool_error | tool_denied | max_iterations
    data: dict[str, Any] = field(default_factory=dict)


EventCallback = Callable[[AgentEvent], None]

# Callback de confirmação para tools destrutivas.
# Recebe (nome_da_tool, args_dict) e retorna True/False.
ConfirmCallback = Callable[[str, dict[str, Any]], bool]


class Agent:
    def __init__(
        self,
        client: ChatClient,
        registry: ToolRegistry,
        system_prompt: str,
        max_iterations: int = MAX_ITERATIONS,
        confirm_cb: ConfirmCallback | None = None,
        *,
        runtime_label: str = "main",
        runtime_depth: int | None = None,
    ) -> None:
        self.client = client
        self.registry = registry
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.confirm_cb = confirm_cb
        self.runtime_label = runtime_label
        self.runtime_depth = runtime_depth
        self.history: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

    def reset(self) -> None:
        self.history = [{"role": "system", "content": self.system_prompt}]

    def set_system_prompt(self, prompt: str) -> None:
        """Substitui o system prompt (pra plan mode, por ex). Preserva o resto do histórico."""
        self.system_prompt = prompt
        if self.history and self.history[0].get("role") == "system":
            self.history[0] = {"role": "system", "content": prompt}
        else:
            self.history.insert(0, {"role": "system", "content": prompt})

    # ------------------------------------------------------------------
    # Modo síncrono — espera a resposta completa antes de devolver.
    # ------------------------------------------------------------------
    def run(self, user_message: str, on_event: EventCallback | None = None) -> str:
        with _runtime.activate(
            client=self.client,
            registry=self.registry,
            label=self.runtime_label,
            depth=self.runtime_depth,
        ):
            return self._run_inner(user_message, on_event)

    def _run_inner(self, user_message: str, on_event: EventCallback | None) -> str:
        self.history.append({"role": "user", "content": user_message})
        on_event = on_event or (lambda _e: None)
        final_text = ""

        for _ in range(self.max_iterations):
            try:
                msg = self.client.chat(
                    self.history, tools=self._tool_schemas()
                )
            except ChatClientError:
                self.history.pop()
                raise

            content = msg.get("content", "") or ""
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls and content:
                extracted, leftover = _extract_tool_calls_from_content(content)
                if extracted:
                    tool_calls = extracted
                    content = leftover

            self._append_assistant(content, tool_calls)

            if content and not tool_calls:
                # Auto-recuperação: se o texto cheira a tool call mas o parser
                # falhou, força o modelo a retentar.
                if _looks_like_tool_call_attempt(content):
                    on_event(AgentEvent("tool_error", {"name": "?", "error": "JSON malformado no texto"}))
                    self.history.append({"role": "user", "content": _RECOVERY_HINT})
                    continue
                on_event(AgentEvent("assistant_text", {"text": content}))
                final_text = content

            if not tool_calls:
                return final_text

            self._execute_tool_calls(tool_calls, on_event)
            final_text = content or final_text

        on_event(AgentEvent("max_iterations", {"limit": self.max_iterations}))
        return final_text or f"(limite de {self.max_iterations} iterações atingido)"

    # ------------------------------------------------------------------
    # Modo streaming — emite text_delta conforme os tokens chegam.
    # ------------------------------------------------------------------
    def run_streaming(
        self, user_message: str, on_event: EventCallback | None = None
    ) -> str:
        with _runtime.activate(
            client=self.client,
            registry=self.registry,
            label=self.runtime_label,
            depth=self.runtime_depth,
        ):
            return self._run_streaming_inner(user_message, on_event)

    def _run_streaming_inner(
        self, user_message: str, on_event: EventCallback | None
    ) -> str:
        self.history.append({"role": "user", "content": user_message})
        on_event = on_event or (lambda _e: None)
        final_text = ""

        for _ in range(self.max_iterations):
            try:
                content, tool_calls = self._consume_stream(on_event)
            except ChatClientError:
                self.history.pop()
                raise

            # Fallback: se não veio tool_calls estruturado, tenta parsear do content
            streamed_was_tool_call = False
            if not tool_calls and content:
                extracted, leftover = _extract_tool_calls_from_content(content)
                if extracted:
                    tool_calls = extracted
                    # Se o stream visual já mostrou esse JSON e ele virou
                    # tool call, manda um text_clear pro frontend apagar.
                    if len(content) - len(leftover) > 20:
                        streamed_was_tool_call = True
                    content = leftover
            if streamed_was_tool_call:
                on_event(AgentEvent("text_clear", {}))

            self._append_assistant(content, tool_calls)

            if content and not tool_calls:
                # Auto-recuperação: o texto cheira a tool call malformada?
                # Força o modelo a retentar em vez de devolver lixo pro usuário.
                if _looks_like_tool_call_attempt(content):
                    on_event(
                        AgentEvent(
                            "tool_error",
                            {"name": "?", "error": "JSON malformado no texto, retentando..."},
                        )
                    )
                    self.history.append(
                        {"role": "user", "content": _RECOVERY_HINT}
                    )
                    continue
                final_text = content

            if not tool_calls:
                return final_text

            on_event(AgentEvent("assistant_text_end", {}))
            self._execute_tool_calls(tool_calls, on_event)
            final_text = content or final_text

        on_event(AgentEvent("max_iterations", {"limit": self.max_iterations}))
        return final_text or f"(limite de {self.max_iterations} iterações atingido)"

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------
    def _tool_schemas(self) -> list[dict[str, Any]]:
        """Retorna schemas em modo slim quando o registry suportar.

        Modelos locais Q4 ficam muito mais responsivos com schemas curtos.
        Se o registry foi monkey-patched (web UI filtrando disabled tools),
        respeita a assinatura sem ``slim`` como fallback.
        """
        try:
            return self.registry.schemas(slim=True)  # type: ignore[call-arg]
        except TypeError:
            return self.registry.schemas()

    def _consume_stream(
        self, on_event: EventCallback
    ) -> tuple[str, list[dict[str, Any]]]:
        """Lê os chunks SSE do client e acumula content + tool_calls parciais.

        Retorna (content_final, tool_calls_assembled).
        """
        content = ""
        tool_calls_by_idx: dict[int, dict[str, Any]] = {}
        # Estado pra suprimir streaming visual quando o modelo começa a
        # emitir um bloco ```json (provável tool call em texto). Sem isso
        # o usuário vê a JSON crua piscar na tela antes do parser final
        # extrair e remover.
        suppress_visual = False

        for chunk in self.client.chat_stream(
            self.history, tools=self._tool_schemas()
        ):
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {}) or {}

            # Texto
            content_delta = delta.get("content")
            if content_delta:
                content += content_delta
                # Detecta tool call mascarada — fence ```json OU JSON cru
                # começando com `{`. O suppress envia text_clear pro UI
                # apagar o que já foi mostrado.
                if not suppress_visual:
                    stripped = content.lstrip()
                    if stripped.startswith("{") and len(stripped) >= 2:
                        # JSON cru no início = quase certeza de tool call
                        # mascarada (resposta normal nunca começa com `{`).
                        suppress_visual = True
                    elif _looks_like_inline_tool_call(content):
                        suppress_visual = True
                    if suppress_visual:
                        on_event(AgentEvent("text_suppress_start", {}))
                if not suppress_visual:
                    on_event(AgentEvent("text_delta", {"delta": content_delta}))

            # Tool calls parciais (formato OpenAI streaming)
            for tc_delta in delta.get("tool_calls") or []:
                idx = tc_delta.get("index", 0)
                if idx not in tool_calls_by_idx:
                    tool_calls_by_idx[idx] = {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                accum = tool_calls_by_idx[idx]
                if tc_delta.get("id"):
                    accum["id"] = tc_delta["id"]
                fn = tc_delta.get("function") or {}
                if fn.get("name"):
                    accum["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    accum["function"]["arguments"] += fn["arguments"]

        tool_calls = list(tool_calls_by_idx.values())
        return content, tool_calls

    def _append_assistant(
        self, content: str, tool_calls: list[dict[str, Any]]
    ) -> None:
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.history.append(msg)

    def _execute_tool_calls(
        self, tool_calls: list[dict[str, Any]], on_event: EventCallback
    ) -> None:
        """Executa cada tool call, respeitando confirmação de destrutivas,
        e acrescenta os resultados ao histórico.
        """
        for tc in tool_calls:
            fn = tc.get("function", {}) or {}
            name = fn.get("name", "")
            tool_call_id = tc.get("id") or f"call_{name}"
            raw_args = fn.get("arguments", {}) or {}
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    raw_args = {}

            on_event(AgentEvent("tool_call", {"name": name, "args": raw_args}))

            # Busca a tool e checa se é destrutiva
            try:
                tool = self.registry.get(name)
            except ToolError as exc:
                result = f"ERRO: {exc}"
                on_event(AgentEvent("tool_error", {"name": name, "error": str(exc)}))
                self._append_tool_result(tool_call_id, name, result)
                continue

            # Confirmação pra tools destrutivas
            if tool.destructive and self.confirm_cb is not None:
                if not self.confirm_cb(name, raw_args):
                    result = "usuário recusou a execução desta tool."
                    on_event(AgentEvent("tool_denied", {"name": name}))
                    self._append_tool_result(tool_call_id, name, result)
                    continue

            # Executa
            try:
                result = tool.call(raw_args)
                on_event(
                    AgentEvent("tool_result", {"name": name, "result": result})
                )
            except ToolError as exc:
                result = f"ERRO: {exc}"
                on_event(
                    AgentEvent("tool_error", {"name": name, "error": str(exc)})
                )

            self._append_tool_result(tool_call_id, name, result)

    def _append_tool_result(self, tool_call_id: str, name: str, content: str) -> None:
        self.history.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": name,
                "content": content,
            }
        )


# ----------------------------------------------------------------------
# Heurística de detecção: o texto cheira a tentativa de tool call?
# ----------------------------------------------------------------------
def _looks_like_tool_call_attempt(text: str) -> bool:
    """True se o texto parece ter JSON de tool call (mesmo malformado)."""
    if not text:
        return False
    # Precisa ter ao menos um '{' E uma chave conhecida de tool call
    if "{" not in text:
        return False
    return bool(_TOOL_CALL_SMELL_RE.search(text))


# Heurística mais específica pra streaming: detecta SE estamos no meio de
# uma tool call mascarada (com ou sem ```json fence). Usado pra suprimir
# text_delta visual antes do parser final extrair.
_INLINE_TC_FENCED_RE = re.compile(
    r"```(?:json|tool_call)?\s*\{[^}]{0,200}?[\"'](?:name|function_name|tool|nome)[\"']",
    re.IGNORECASE | re.DOTALL,
)
# Versão "JSON cru" sem fence: começa com `{` (após whitespace opcional)
# e tem `"name"`/`"function"`/`"tool"` em até ~150 chars.
_INLINE_TC_RAW_RE = re.compile(
    r"^\s*\{[^}]{0,200}?[\"'](?:name|function_name|tool|nome)[\"']",
    re.IGNORECASE | re.DOTALL,
)


def _looks_like_inline_tool_call(text: str) -> bool:
    """True se ``text`` (parcial, mid-stream) parece estar dentro de uma
    tool call mascarada — seja em bloco ```json ou JSON cru no início.
    """
    if not text:
        return False
    if "```" in text and _INLINE_TC_FENCED_RE.search(text):
        return True
    # JSON cru no começo do conteúdo (caso clássico do Qwen Q4 alucinando
    # tool calls quando o pedido é trivial tipo "olá").
    if "{" in text and _INLINE_TC_RAW_RE.search(text):
        return True
    return False


# ----------------------------------------------------------------------
# Fallback parser — para modelos que emitem tool calls como JSON no content
# ----------------------------------------------------------------------
def _extract_tool_calls_from_content(content: str) -> tuple[list[dict[str, Any]], str]:
    """Tenta extrair tool calls de um content textual.

    Cobre formatos comuns que modelos open-source emitem:
      1. <tool_call>{"name": "...", "arguments": {...}}</tool_call>  (template Qwen)
      2. JSON nu no content: {"name": "...", "arguments": {...}}
      3. Array JSON: [{"name": "..."}, {"name": "..."}]
      4. Múltiplos objetos concatenados sem separador (Qwen Q4 às vezes faz)
      5. JSON dentro de code fences ```json ... ```

    Retorna (tool_calls, leftover_text). Apenas objetos com `name` são tratados
    como tool calls; os demais viram parte do leftover_text.
    """
    tag_matches = _TOOL_CALL_TAG_RE.findall(content)
    if tag_matches:
        calls: list[dict[str, Any]] = []
        for raw in tag_matches:
            parsed = _parse_call_json(raw)
            if parsed:
                calls.append(parsed)
        leftover = _TOOL_CALL_TAG_RE.sub("", content).strip()
        if calls:
            return calls, leftover

    # Extrai TODOS os blocos ```json ... ``` do texto (modelos costumam emitir
    # várias tool calls intercaladas com prosa)
    fence_re = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
    fence_matches = fence_re.findall(content)
    if fence_matches:
        calls: list[dict[str, Any]] = []
        for raw in fence_matches:
            parsed = _parse_call_json(raw)
            if parsed:
                calls.append(parsed)
        if calls:
            leftover = fence_re.sub("", content).strip()
            return calls, leftover

    # Remove code fences se presentes (caso simples sem texto extra)
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```\s*$", "", stripped)

    # Tenta primeiro parsear o content INTEIRO como JSON único (caminho mais limpo)
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            parsed = _normalize_call_dict(data)
            return ([parsed], "") if parsed else ([], content)
        if isinstance(data, list):
            all_calls = [
                c
                for c in (_normalize_call_dict(d) for d in data if isinstance(d, dict))
                if c
            ]
            return (all_calls, "") if all_calls else ([], content)
    except json.JSONDecodeError:
        pass

    # Fallback: extrair múltiplos objetos JSON concatenados do content
    # (caso em que Qwen Q4 emite `{...}{...}` sem separador)
    objects = _extract_top_level_json_objects(stripped)
    if objects:
        calls: list[dict[str, Any]] = []
        remaining_text_parts: list[str] = []
        for obj_str in objects:
            try:
                data = json.loads(obj_str)
            except json.JSONDecodeError:
                remaining_text_parts.append(obj_str)
                continue
            if isinstance(data, dict):
                parsed = _normalize_call_dict(data)
                if parsed:
                    calls.append(parsed)
                    continue
                # Dict sem `name` — pode ser resposta estruturada do modelo
                # Usa o valor de "message"/"content"/"text" se houver
                msg = (
                    data.get("message")
                    or data.get("content")
                    or data.get("text")
                )
                if isinstance(msg, str):
                    remaining_text_parts.append(msg)
                else:
                    remaining_text_parts.append(obj_str)
            else:
                remaining_text_parts.append(obj_str)
        if calls:
            return calls, "\n".join(remaining_text_parts).strip()

    return [], content


def _extract_top_level_json_objects(text: str) -> list[str]:
    """Extrai objetos JSON top-level de um texto, contando chaves balanceadas.

    Retorna lista de substrings '{...}' que parecem objetos JSON válidos.
    Útil quando o modelo emite múltiplos objetos concatenados sem vírgula.
    """
    objects: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                objects.append(text[start : i + 1])
                start = -1
    return objects


def _parse_call_json(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return _normalize_call_dict(data)


def _normalize_call_dict(data: dict[str, Any]) -> dict[str, Any] | None:
    # Aceita variantes que modelos open-source emitem por engano:
    # name | function_name | tool | tool_name | nome
    name = (
        data.get("name")
        or data.get("function_name")
        or data.get("tool")
        or data.get("tool_name")
        or data.get("nome")
    )
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    # Aceita variantes pra args:
    # arguments | parameters | args | input | argumentos
    arguments = (
        data.get("arguments")
        if data.get("arguments") is not None
        else data.get("parameters")
        if data.get("parameters") is not None
        else data.get("args")
        if data.get("args") is not None
        else data.get("input")
        if data.get("input") is not None
        else data.get("argumentos", {})
    )
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    # Desfaz indireção meta-tool antiga: se o modelo ainda tenta chamar
    # `usar_habilidade(nome=X, argumentos=Y)`, redireciona pra X(Y).
    if name in ("usar_habilidade", "use_skill") and isinstance(arguments, dict):
        inner_name = arguments.get("nome") or arguments.get("name")
        inner_args = arguments.get("argumentos") or arguments.get("arguments") or {}
        if isinstance(inner_name, str) and inner_name.strip():
            name = inner_name.strip()
            arguments = inner_args if isinstance(inner_args, dict) else {}
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }
