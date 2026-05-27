"""Compactação automática de histórico longo do agent.

Quando a conversa cresce demais, o histórico passa a consumir muito contexto
do modelo (e fica lento). Este módulo fornece uma heurística leve de contagem
de tokens e um compactor que pede pro próprio modelo resumir o "meio" da
conversa, preservando o system prompt e as últimas mensagens (que são as mais
relevantes pro próximo turno).
"""
from __future__ import annotations

import json
from typing import Any

from mtzcode.client import ChatClient

# Prefixo usado pra marcar a mensagem-resumo injetada no histórico — facilita
# detectar / não recompactar o que já foi compactado.
SUMMARY_PREFIX = "[resumo da conversa anterior]: "

# Prompt enviado ao modelo pra produzir o resumo.
_SUMMARY_INSTRUCTION = (
    "Resuma os principais fatos, decisões, arquivos tocados e erros "
    "encontrados nesta conversa em até 400 palavras."
)


def estimate_tokens(messages: list[dict]) -> int:
    """Estimativa grosseira de tokens consumidos por uma lista de mensagens.

    Heurística: ~4 caracteres por token (vale pra inglês e PT-BR latino).
    Inclui content textual e também serialização de tool_calls / tool results
    pra não subestimar conversas pesadas em tooling.
    """
    total_chars = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif content is not None:
            # content pode ser lista (multimodal) ou dict — serializa
            try:
                total_chars += len(json.dumps(content, ensure_ascii=False))
            except (TypeError, ValueError):
                total_chars += len(str(content))

        tool_calls = msg.get("tool_calls")
        if tool_calls:
            try:
                total_chars += len(json.dumps(tool_calls, ensure_ascii=False))
            except (TypeError, ValueError):
                total_chars += len(str(tool_calls))

    return total_chars // 4


def should_compact(messages: list[dict], max_tokens: int = 12000) -> bool:
    """Diz se vale a pena compactar o histórico agora."""
    return estimate_tokens(messages) >= max_tokens


class HistoryCompactor:
    """Compacta o histórico do agent usando o próprio LLM como sumarizador.

    Estratégia:
      - Mantém intacta a primeira mensagem se for system prompt.
      - Mantém intactas as últimas `keep_last` mensagens (contexto recente).
      - Pede pro client um resumo PT-BR de tudo que ficou no meio.
      - Substitui o bloco do meio por uma única mensagem role=system com
        prefixo `[resumo da conversa anterior]: ...`.
    """

    def __init__(
        self,
        client: ChatClient,
        keep_last: int = 6,
        target_tokens: int = 4000,
    ) -> None:
        self.client = client
        self.keep_last = keep_last
        self.target_tokens = target_tokens

    def compact(self, messages: list[dict]) -> list[dict]:
        """Devolve uma nova lista de mensagens compactada.

        Se não houver nada significativo pra compactar (histórico curto ou
        sem "meio"), devolve a lista original sem chamar o modelo.
        """
        if not messages:
            return messages

        # Detecta system prompt no início.
        head: list[dict[str, Any]] = []
        body_start = 0
        if messages[0].get("role") == "system":
            head = [messages[0]]
            body_start = 1

        body = messages[body_start:]
        if len(body) <= self.keep_last:
            # Nada de "meio" pra resumir.
            return list(messages)

        middle = body[: -self.keep_last] if self.keep_last > 0 else body
        tail = body[-self.keep_last :] if self.keep_last > 0 else []

        if not middle:
            return list(messages)

        # Garante que o tail comece em um ponto consistente — se a primeira
        # mensagem do tail for um `tool` result órfão (sem o assistant que a
        # disparou), puxa também o assistant correspondente pro tail pra não
        # quebrar o contrato da API.
        while tail and tail[0].get("role") == "tool" and middle:
            tail.insert(0, middle.pop())

        if not middle:
            return list(messages)

        summary_text = self._summarize(middle)
        summary_msg: dict[str, Any] = {
            "role": "system",
            "content": SUMMARY_PREFIX + summary_text,
        }

        return head + [summary_msg] + tail

    # ------------------------------------------------------------------
    def _summarize(self, middle: list[dict]) -> str:
        """Pede ao client um resumo PT-BR do trecho do meio."""
        transcript = _render_transcript(middle)
        prompt_messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "Você é um assistente que resume conversas técnicas em PT-BR "
                    "de forma factual e concisa."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"{_SUMMARY_INSTRUCTION}\n\n"
                    "Conversa a resumir (turnos entre usuário, assistente e tools):\n"
                    f"{transcript}"
                ),
            },
        ]
        try:
            reply = self.client.chat(prompt_messages, tools=None)
        except Exception as exc:  # pragma: no cover - fallback defensivo
            return f"(falha ao gerar resumo automático: {exc})"

        content = reply.get("content") if isinstance(reply, dict) else None
        if not isinstance(content, str) or not content.strip():
            return "(resumo indisponível)"
        return content.strip()


def _render_transcript(messages: list[dict]) -> str:
    """Serializa um trecho de histórico em texto plano pro sumarizador ler."""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content") or ""
        if not isinstance(content, str):
            try:
                content = json.dumps(content, ensure_ascii=False)
            except (TypeError, ValueError):
                content = str(content)

        tool_calls = msg.get("tool_calls")
        if tool_calls:
            try:
                tc_str = json.dumps(tool_calls, ensure_ascii=False)
            except (TypeError, ValueError):
                tc_str = str(tool_calls)
            content = f"{content}\n[tool_calls]: {tc_str}".strip()

        if role == "tool":
            name = msg.get("name", "")
            parts.append(f"[tool:{name}] {content}")
        else:
            parts.append(f"[{role}] {content}")
    return "\n".join(parts)


def maybe_compact(
    agent: Any,
    compactor: HistoryCompactor,
    max_tokens: int = 12000,
) -> bool:
    """Helper pro CLI: compacta `agent.history` in-place se passou do limite.

    Retorna True se compactou (útil pra avisar o usuário), False caso contrário.
    """
    history = getattr(agent, "history", None)
    if not isinstance(history, list):
        return False
    if not should_compact(history, max_tokens=max_tokens):
        return False
    new_history = compactor.compact(history)
    if new_history is history or len(new_history) >= len(history):
        return False
    agent.history = new_history
    return True
