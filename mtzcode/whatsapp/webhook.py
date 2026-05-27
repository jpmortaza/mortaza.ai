"""Router FastAPI para receber eventos da Evolution API.

Endpoints:

  POST /whatsapp/webhook       — recebe eventos da Evolution (messages.upsert)
  POST /whatsapp/send          — enviar mensagem manualmente (debug)
  GET  /whatsapp/health        — status do serviço
  POST /whatsapp/reset/{jid}   — resetar histórico do contato

O processamento da mensagem (chamada do agente + resposta) acontece em uma
task background para que o webhook responda 200 rapidamente — Evolution
retenta se demorar muito.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from mtzcode.whatsapp.evolution import EvolutionClient, EvolutionError
from mtzcode.whatsapp.session import SessionManager, _digits


log = logging.getLogger("mtzcode.whatsapp")

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


# --------------------------------------------------------------------- setup
def setup(app, session_manager: SessionManager) -> None:
    """Anexa o manager e o cliente Evolution ao `app.state` e monta o router."""
    app.state.wa_sessions = session_manager
    app.state.wa_evolution = EvolutionClient()
    app.state.wa_webhook_secret = os.environ.get("WHATSAPP_WEBHOOK_SECRET", "")
    allowed = os.environ.get("WHATSAPP_ALLOWED_NUMBERS", "").strip()
    app.state.wa_allowed = {
        _digits(n) for n in allowed.split(",") if n.strip()
    } if allowed else None
    app.include_router(router)


# ------------------------------------------------------------------ endpoints
@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    ev: EvolutionClient = request.app.state.wa_evolution
    sm: SessionManager = request.app.state.wa_sessions
    return {
        "ok": True,
        "evolution_instance": ev.instance,
        "evolution_base_url": ev.base_url,
        "active_sessions": len(sm._sessions),
        "allowed_numbers_configured": request.app.state.wa_allowed is not None,
        "auto_confirm": sm.auto_confirm,
        "profile": sm.cfg.profile.name,
    }


@router.post("/send")
async def send_manual(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Envio manual — útil para teste sem precisar do webhook."""
    number = payload.get("number") or payload.get("to")
    text = payload.get("text") or payload.get("message")
    if not number or not text:
        raise HTTPException(400, "campos 'number' e 'text' são obrigatórios")
    ev: EvolutionClient = request.app.state.wa_evolution
    try:
        result = ev.send_text(number, text)
    except EvolutionError as exc:
        raise HTTPException(502, str(exc))
    return {"ok": True, "result": result}


@router.post("/reset/{jid}")
async def reset_session(request: Request, jid: str) -> dict[str, Any]:
    sm: SessionManager = request.app.state.wa_sessions
    sm.reset(jid)
    return {"ok": True, "jid": jid}


@router.post("/webhook")
async def webhook(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    secret = request.app.state.wa_webhook_secret
    if secret and x_webhook_secret != secret:
        raise HTTPException(401, "secret inválido")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "payload não é JSON")

    parsed = _parse_evolution_event(payload)
    if parsed is None:
        # Eventos não-mensagem (status, presence, etc) — apenas reconhece.
        return {"ok": True, "ignored": True}

    jid, text, push_name = parsed
    allowed = request.app.state.wa_allowed
    if allowed is not None and _digits(jid) not in allowed:
        log.info("whatsapp: número não autorizado: %s", jid)
        return {"ok": True, "ignored": "not_allowed"}

    # Processa em background — evita timeout do webhook.
    asyncio.create_task(_handle_message(request.app, jid, text, push_name))
    return {"ok": True, "queued": True}


# ----------------------------------------------------------------- internals
def _parse_evolution_event(
    payload: dict[str, Any],
) -> tuple[str, str, str] | None:
    """Extrai (jid, text, pushName) de um payload Evolution.

    Aceita o formato `messages.upsert` (mais comum). Ignora eventos onde
    `fromMe=true`, mensagens vazias, ou tipos não-texto.
    """
    event = payload.get("event") or ""
    if event and "message" not in event.lower():
        return None
    data = payload.get("data") or payload
    if isinstance(data, list):
        data = data[0] if data else {}

    key = data.get("key") or {}
    if key.get("fromMe"):
        return None
    jid = key.get("remoteJid") or data.get("remoteJid") or ""
    if not jid or "@g.us" in jid:
        # Ignora grupos por enquanto — só DM 1-a-1.
        return None

    message = data.get("message") or {}
    text = (
        message.get("conversation")
        or (message.get("extendedTextMessage") or {}).get("text")
        or (message.get("ephemeralMessage") or {}).get("message", {}).get("conversation")
        or ""
    ).strip()
    if not text:
        return None

    push_name = data.get("pushName") or ""
    return jid, text, push_name


async def _handle_message(app, jid: str, text: str, push_name: str) -> None:
    sm: SessionManager = app.state.wa_sessions
    ev: EvolutionClient = app.state.wa_evolution

    # Comandos especiais
    cmd = text.strip().lower()
    if cmd in ("/reset", "/limpar", "reset!", "/clear"):
        sm.reset(jid)
        await _safe_send(ev, jid, "🧹 histórico zerado. Pode mandar a próxima.")
        return
    if cmd in ("/help", "/ajuda", "/menu"):
        await _safe_send(
            ev,
            jid,
            "Comandos:\n"
            "/reset — apaga o histórico desta conversa\n"
            "/help — mostra este menu\n\n"
            "Manda qualquer outra coisa que eu respondo.",
        )
        return

    sess = sm.get(jid)
    # Mostra "digitando…" pra dar feedback antes da resposta.
    try:
        ev.send_typing(jid, duration_ms=2500)
    except Exception:
        pass

    user_msg = text if not push_name else f"[{push_name}] {text}"

    try:
        reply_text = await asyncio.to_thread(_run_agent, sess, user_msg)
    except Exception as exc:
        log.exception("erro processando mensagem WhatsApp")
        reply_text = f"⚠️ erro interno: {exc}"

    reply_text = (reply_text or "").strip() or "(sem resposta)"
    # WhatsApp não tem hard limit visível, mas split em chunks grandes evita
    # truncamento em alguns clientes. 4000 chars é seguro.
    for chunk in _chunk(reply_text, 4000):
        await _safe_send(ev, jid, chunk)


def _run_agent(sess, user_message: str) -> str:
    """Executa o loop síncrono do agente e devolve a resposta final em texto."""
    return sess.agent.run(user_message)


async def _safe_send(ev: EvolutionClient, jid: str, text: str) -> None:
    try:
        await asyncio.to_thread(ev.send_text, jid, text)
    except EvolutionError as exc:
        log.error("falha ao enviar pra %s: %s", jid, exc)


def _chunk(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    out: list[str] = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > size and cur:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out
