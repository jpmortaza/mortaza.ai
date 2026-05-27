"""Cliente Anthropic (Claude) com a mesma interface do ChatClient.

Traduz mensagens/tools entre o formato OpenAI (usado internamente pelo agent.py
e pelo histórico persistido) e o formato Messages API da Anthropic.

Implementa `chat()` (síncrono) e `chat_stream()` (SSE). Ambos devolvem dicts
no formato OpenAI pra que o agent loop não precise saber qual backend está
em uso.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Iterable, Iterator

import httpx

from mtzcode.client import ChatClientError
from mtzcode.profiles import Profile


_ANTHROPIC_VERSION = "2023-06-01"
_TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504, 529}
_TRANSIENT_EXC = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


class AnthropicClient:
    """Mesma superfície do `ChatClient`, mas falando Messages API."""

    def __init__(
        self,
        profile: Profile,
        timeout_s: float = 300.0,
        connect_timeout_s: float = 10.0,
        max_retries: int = 2,
    ) -> None:
        self.profile = profile
        self.model = profile.model
        self._max_retries = max(0, int(max_retries))

        api_key = os.environ.get(profile.api_key_env or "ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ChatClientError(
                f"variável de ambiente {profile.api_key_env or 'ANTHROPIC_API_KEY'} "
                f"não definida — necessária para usar {profile.label}."
            )

        timeout = httpx.Timeout(timeout_s, connect=connect_timeout_s)
        self._client = httpx.Client(
            base_url=profile.base_url,
            timeout=timeout,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )

    # ------------------------------------------------------------------ chat
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        system, anth_messages = _to_anthropic_messages(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": anth_messages,
            "max_tokens": int(os.environ.get("MTZCODE_MAX_TOKENS", "4096")),
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = _to_anthropic_tools(tools)

        response = self._post_with_retry("/v1/messages", payload)
        if response.status_code != 200:
            raise ChatClientError(
                f"{self.profile.label} respondeu {response.status_code}: "
                f"{(response.text or '')[:500]}"
            )
        return _from_anthropic_response(response.json())

    # --------------------------------------------------------------- stream
    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: Iterable[dict[str, Any]] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Emite chunks no formato OpenAI streaming: choices[0].delta."""
        system, anth_messages = _to_anthropic_messages(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": anth_messages,
            "max_tokens": int(os.environ.get("MTZCODE_MAX_TOKENS", "4096")),
            "stream": True,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = _to_anthropic_tools(tools)

        try:
            with self._client.stream("POST", "/v1/messages", json=payload) as resp:
                if resp.status_code != 200:
                    try:
                        body = resp.read().decode("utf-8", errors="replace")
                    except Exception:
                        body = "<erro lendo body>"
                    raise ChatClientError(
                        f"{self.profile.label} respondeu {resp.status_code}: "
                        f"{body[:500]}"
                    )
                yield from _stream_anthropic_to_openai(resp.iter_lines())
        except httpx.HTTPError as exc:
            raise ChatClientError(
                f"falha ao conectar em {self.profile.base_url}: {exc}"
            ) from exc

    # ----------------------------------------------------------------- util
    def _post_with_retry(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.post(path, json=payload)
                if resp.status_code in _TRANSIENT_STATUS and attempt < self._max_retries:
                    time.sleep(min(8.0, 0.5 * (2 ** attempt)))
                    continue
                return resp
            except _TRANSIENT_EXC as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    break
                time.sleep(min(8.0, 0.5 * (2 ** attempt)))
            except httpx.HTTPError as exc:
                raise ChatClientError(
                    f"falha ao conectar em {self.profile.base_url}: {exc}"
                ) from exc
        raise ChatClientError(
            f"falha ao conectar em {self.profile.base_url} após "
            f"{self._max_retries + 1} tentativa(s): {last_exc}"
        ) from last_exc

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AnthropicClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# ============================================================ tradutores
def _to_anthropic_tools(tools: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI tools → Anthropic tools."""
    out: list[dict[str, Any]] = []
    for t in tools:
        fn = t.get("function") or t
        out.append(
            {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


def _to_anthropic_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """OpenAI messages → (system, anthropic messages).

    - `system` vira parâmetro top-level (concatena se houver múltiplos).
    - `tool` messages viram `tool_result` dentro de uma user message.
    - `assistant` com `tool_calls` vira content array com `tool_use` blocks.
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def _flush_tool_results():
        if pending_tool_results:
            out.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for m in messages:
        role = m.get("role")
        if role == "system":
            content = m.get("content") or ""
            if isinstance(content, str) and content.strip():
                system_parts.append(content)
            continue
        if role == "tool":
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or m.get("id") or "unknown",
                    "content": _coerce_text(m.get("content")),
                }
            )
            continue
        _flush_tool_results()
        if role == "user":
            out.append({"role": "user", "content": _coerce_text(m.get("content"))})
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = m.get("content")
            if isinstance(text, str) and text.strip():
                blocks.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except json.JSONDecodeError:
                        args = {"_raw": args}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id") or f"call_{fn.get('name', 'x')}",
                        "name": fn.get("name") or "unknown",
                        "input": args or {},
                    }
                )
            if not blocks:
                blocks = [{"type": "text", "text": ""}]
            out.append({"role": "assistant", "content": blocks})
    _flush_tool_results()
    return "\n\n".join(system_parts), out


def _coerce_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # OpenAI multimodal — extrai só texto pra simplificar.
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text", "")))
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    return str(content)


def _from_anthropic_response(data: dict[str, Any]) -> dict[str, Any]:
    """Resposta Messages API → message dict no formato OpenAI."""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in data.get("content") or []:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id") or f"call_{block.get('name', 'x')}",
                    "type": "function",
                    "function": {
                        "name": block.get("name") or "unknown",
                        "arguments": json.dumps(
                            block.get("input") or {}, ensure_ascii=False
                        ),
                    },
                }
            )
    msg: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _stream_anthropic_to_openai(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Converte SSE da Anthropic → chunks no formato OpenAI streaming.

    Eventos relevantes:
      - content_block_start: começa um bloco (text ou tool_use).
      - content_block_delta: text_delta ou input_json_delta.
      - content_block_stop: encerra o bloco.
      - message_stop: fim.
    """
    cur_tool_idx: int | None = None
    cur_tool_id: str | None = None
    cur_tool_name: str | None = None
    cur_tool_args_buf: list[str] = []

    for raw in lines:
        if not raw or not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            evt = json.loads(payload)
        except json.JSONDecodeError:
            continue

        etype = evt.get("type")
        if etype == "content_block_start":
            block = evt.get("content_block") or {}
            if block.get("type") == "tool_use":
                cur_tool_idx = evt.get("index", 0)
                cur_tool_id = block.get("id")
                cur_tool_name = block.get("name")
                cur_tool_args_buf = []
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": cur_tool_idx,
                                        "id": cur_tool_id,
                                        "type": "function",
                                        "function": {
                                            "name": cur_tool_name,
                                            "arguments": "",
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
        elif etype == "content_block_delta":
            delta = evt.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                yield {
                    "choices": [{"delta": {"content": delta.get("text", "")}}]
                }
            elif dtype == "input_json_delta" and cur_tool_idx is not None:
                partial = delta.get("partial_json", "")
                cur_tool_args_buf.append(partial)
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": cur_tool_idx,
                                        "function": {"arguments": partial},
                                    }
                                ]
                            }
                        }
                    ]
                }
        elif etype == "content_block_stop":
            cur_tool_idx = None
            cur_tool_id = None
            cur_tool_name = None
            cur_tool_args_buf = []
        elif etype == "message_stop":
            return
