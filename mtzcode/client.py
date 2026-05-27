"""Cliente HTTP unificado para qualquer backend OpenAI-compatible.

Suporta Ollama local (via `/v1/chat/completions`) e serviços cloud como Groq.
A escolha do backend é feita pelo `Profile` passado no construtor.
"""
from __future__ import annotations

import copy
import json
import os
import time
from typing import Any, Iterable

import httpx


# Status codes considerados transientes (vale a pena retry com backoff)
_TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
# Exceções de rede transientes
_TRANSIENT_EXC = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)

from mtzcode.profiles import Profile


class ChatClientError(RuntimeError):
    """Erro de comunicação com o backend (rede, auth, payload, etc)."""


# Alias retrocompatível pro código antigo (agent.py importava OllamaError).
OllamaError = ChatClientError


class ChatClient:
    """Cliente OpenAI-compatible. Funciona com Ollama, Groq, ou qualquer outro
    serviço que exponha `/chat/completions` no formato OpenAI.
    """

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

        api_key = "ollama"  # placeholder — Ollama não verifica
        if profile.needs_api_key:
            api_key = os.environ.get(profile.api_key_env or "", "")
            if not api_key:
                raise ChatClientError(
                    f"variável de ambiente {profile.api_key_env} não definida — "
                    f"necessária para usar {profile.label}."
                )

        # Timeout fino: connect curto, read longo (Ollama pode demorar pra "aquecer" modelo)
        timeout = httpx.Timeout(timeout_s, connect=connect_timeout_s)
        self._client = httpx.Client(
            base_url=profile.base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Envia uma rodada e devolve o `message` do assistant (dict)."""
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "messages": _normalize_messages_for_openai(messages),
            "stream": False,
        }
        if tools:
            payload["tools"] = list(tools)
        self._inject_backend_options(payload)

        response = self._post_with_retry("/chat/completions", payload)
        if response.status_code != 200:
            raise ChatClientError(
                self._format_http_error(response.status_code, response.text)
            )

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise ChatClientError(f"resposta sem choices: {str(data)[:300]}")
        return choices[0].get("message", {}) or {}

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: Iterable[dict[str, Any]] | None = None,
    ):
        """Gera chunks SSE do endpoint `/chat/completions` com `stream=true`.

        Cada item yield-ado é um dict no formato OpenAI streaming, com `choices[0].delta`
        contendo `content` e/ou `tool_calls` parciais.
        """
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "messages": _normalize_messages_for_openai(messages),
            "stream": True,
        }
        if tools:
            payload["tools"] = list(tools)
        self._inject_backend_options(payload)

        try:
            with self._client.stream("POST", "/chat/completions", json=payload) as response:
                if response.status_code != 200:
                    # Tenta ler o body de erro
                    try:
                        body = response.read().decode("utf-8", errors="replace")
                    except Exception:
                        body = "<erro lendo body>"
                    raise ChatClientError(
                        self._format_http_error(response.status_code, body)
                    )
                for line in response.iter_lines():
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            yield json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
        except httpx.HTTPError as exc:
            raise ChatClientError(
                f"falha ao conectar em {self.profile.base_url}: {exc}"
            ) from exc

    def _post_with_retry(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        """POST com retry exponencial para erros transientes (rede + 5xx/429).

        Não tenta de novo em 4xx (exceto 408/425/429): erro do cliente,
        retentativa não muda o resultado.
        """
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.post(path, json=payload)
                if response.status_code in _TRANSIENT_STATUS and attempt < self._max_retries:
                    time.sleep(min(8.0, 0.5 * (2 ** attempt)))
                    continue
                return response
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
            f"falha ao conectar em {self.profile.base_url} após {self._max_retries + 1} tentativa(s): {last_exc}"
        ) from last_exc

    def _inject_backend_options(self, payload: dict[str, Any]) -> None:
        """Injeta opções específicas do backend no payload.

        CRÍTICO pro Ollama: sem `num_ctx` explícito, ele usa 2048 tokens
        e trunca o prompt (system + tools + histórico facilmente passa
        de 4-5k), o que **destrói o KV cache** entre turnos e força o
        modelo a reprocessar TUDO a cada mensagem.

        Lê de ``Settings`` (editável pelo web UI). Env vars MTZCODE_*
        têm precedência pra debug/scripting.

        Cloud (Groq/Maritaca) ignora esses campos silenciosamente.
        """
        if not self.profile.is_local or self.profile.backend != "ollama":
            return
        # Carrega settings persistentes
        try:
            from mtzcode.settings import get_settings
            opts = get_settings().model_options
            num_ctx = int(os.environ.get("MTZCODE_NUM_CTX", opts.num_ctx))
            num_predict = int(
                os.environ.get("MTZCODE_NUM_PREDICT", opts.num_predict)
            )
            keep_alive = os.environ.get("MTZCODE_KEEP_ALIVE", opts.keep_alive)
            temperature = opts.temperature
            top_p = opts.top_p
        except Exception:
            num_ctx = int(os.environ.get("MTZCODE_NUM_CTX", "16384"))
            num_predict = int(os.environ.get("MTZCODE_NUM_PREDICT", "2048"))
            keep_alive = os.environ.get("MTZCODE_KEEP_ALIVE", "30m")
            temperature = 0.3
            top_p = 0.9
        payload.setdefault(
            "options",
            {
                "num_ctx": num_ctx,
                "num_predict": num_predict,
                "temperature": temperature,
                "top_p": top_p,
            },
        )
        payload.setdefault("keep_alive", keep_alive)

    def _format_http_error(self, status: int, body: str) -> str:
        """Constrói mensagem de erro amigável, detectando casos comuns do Ollama."""
        snippet = (body or "")[:500]
        low = snippet.lower()
        # Caso clássico: modelo não puxado no Ollama → 404 "model 'X' not found"
        if (
            self.profile.is_local
            and self.profile.backend == "ollama"
            and status == 404
            and "not found" in low
        ):
            return (
                f"{self.profile.label}: o modelo '{self.profile.model}' não está "
                f"instalado no Ollama. Rode no terminal:\n\n"
                f"    ollama pull {self.profile.model}\n\n"
                f"(uma vez baixado, é só mandar a mensagem de novo)"
            )
        # Modelo instalado mas sem suporte a tool calling (ex: deepseek-coder-v2,
        # deepseek-r1, gemma2 antigo). O mtzcode é um agent loop e DEPENDE de tools.
        if (
            self.profile.is_local
            and self.profile.backend == "ollama"
            and status == 400
            and "does not support tools" in low
        ):
            return (
                f"{self.profile.label}: o modelo '{self.profile.model}' não suporta "
                f"tool calling no Ollama, e o mtzcode precisa de tools pra funcionar. "
                f"Troca de profile pra um modelo compatível:\n\n"
                f"  • qwen-14b   (Qwen 2.5 Coder 14B — recomendado)\n"
                f"  • qwen-7b    (mais leve)\n"
                f"  • llama3.1-8b\n\n"
                f"Use o dropdown de modelo na UI ou /modelo no CLI."
            )
        return f"{self.profile.label} respondeu {status}: {snippet}"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ChatClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# Aliases retrocompatíveis
OllamaClient = ChatClient


def _normalize_messages_for_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A API OpenAI exige `tool_calls[*].function.arguments` como string JSON.
    Algumas mensagens do histórico podem ter sido criadas com arguments como dict
    (especialmente vindas do fallback parser do agent). Reserializa onde precisa.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        # Cópia rasa primeiro; só clona profundo se houver tool_calls que mexer.
        tcs = m.get("tool_calls")
        if not tcs:
            out.append(m)
            continue
        new_msg = copy.deepcopy(m)
        for tc in new_msg.get("tool_calls", []):
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, dict):
                fn["arguments"] = json.dumps(args, ensure_ascii=False)
            elif args is None:
                fn["arguments"] = "{}"
            # OpenAI também exige `type: "function"` e um `id` em cada tool_call.
            tc.setdefault("type", "function")
            tc.setdefault("id", f"call_{fn.get('name', 'unknown')}")
        out.append(new_msg)
    return out


def make_client(
    profile: Profile,
    timeout_s: float = 300.0,
    connect_timeout_s: float = 10.0,
    max_retries: int = 2,
):
    """Factory que devolve o cliente certo pro backend do profile.

    Tudo que o agent loop precisa é da interface `chat()` / `chat_stream()`,
    que ambos os clientes implementam.
    """
    if profile.backend == "anthropic":
        # Import tardio pra evitar dependência circular e custo se não usado.
        from mtzcode.anthropic_client import AnthropicClient
        return AnthropicClient(
            profile,
            timeout_s=timeout_s,
            connect_timeout_s=connect_timeout_s,
            max_retries=max_retries,
        )
    return ChatClient(
        profile,
        timeout_s=timeout_s,
        connect_timeout_s=connect_timeout_s,
        max_retries=max_retries,
    )
