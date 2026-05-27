"""Cliente HTTP para a Evolution API (envio de mensagens WhatsApp).

A Evolution API expõe o endpoint:

    POST {base_url}/message/sendText/{instance}

com header `apikey: <key>` e body `{"number": "<E164 sem +>", "text": "..."}`.

Esta implementação cobre o envio síncrono de texto. Mídia/áudio podem ser
acrescentados depois — o webhook (entrada) é o caminho crítico pro MVP.
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx


class EvolutionError(RuntimeError):
    """Falha de comunicação com a Evolution API."""


_TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}


class EvolutionClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        instance: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.base_url = (base_url or os.environ.get("EVOLUTION_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("EVOLUTION_API_KEY", "")
        self.instance = instance or os.environ.get("EVOLUTION_INSTANCE", "")
        if not self.base_url or not self.api_key or not self.instance:
            raise EvolutionError(
                "EvolutionClient precisa de EVOLUTION_BASE_URL, EVOLUTION_API_KEY "
                "e EVOLUTION_INSTANCE definidos (env ou args)."
            )
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            headers={"apikey": self.api_key, "Content-Type": "application/json"},
        )

    def send_text(self, number: str, text: str, *, delay_ms: int = 0) -> dict[str, Any]:
        """Envia uma mensagem de texto. `number` aceita JID ou E.164 sem '+'."""
        payload: dict[str, Any] = {
            "number": _strip_jid(number),
            "text": text,
        }
        if delay_ms > 0:
            payload["delay"] = int(delay_ms)
        return self._post_retry(f"/message/sendText/{self.instance}", payload)

    def send_typing(self, number: str, duration_ms: int = 2000) -> None:
        """Indicador opcional de 'digitando'. Falhas são silenciadas."""
        try:
            self._client.post(
                f"/chat/sendPresence/{self.instance}",
                json={
                    "number": _strip_jid(number),
                    "presence": "composing",
                    "delay": int(duration_ms),
                },
            )
        except httpx.HTTPError:
            pass

    def _post_retry(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(3):
            try:
                resp = self._client.post(path, json=payload)
                if resp.status_code in _TRANSIENT_STATUS and attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                if resp.status_code >= 400:
                    raise EvolutionError(
                        f"Evolution respondeu {resp.status_code}: {resp.text[:300]}"
                    )
                try:
                    return resp.json()
                except ValueError:
                    return {"raw": resp.text}
            except httpx.HTTPError as exc:
                last = exc
                if attempt >= 2:
                    break
                time.sleep(0.5 * (2 ** attempt))
        raise EvolutionError(f"falha de rede com Evolution após 3 tentativas: {last}")

    def close(self) -> None:
        self._client.close()


def _strip_jid(number: str) -> str:
    """Aceita JIDs ('5511...@s.whatsapp.net') e E.164 sem '+' ('5511...').
    Retorna sempre só os dígitos.
    """
    n = (number or "").split("@")[0]
    return "".join(ch for ch in n if ch.isdigit())
