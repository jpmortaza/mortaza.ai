"""Integração com WhatsApp via Evolution API.

Expõe um router FastAPI (`router`) que pode ser montado no servidor web
existente, e um cliente (`EvolutionClient`) para enviar mensagens.
"""
from mtzcode.whatsapp.evolution import EvolutionClient, EvolutionError
from mtzcode.whatsapp.session import SessionManager
from mtzcode.whatsapp.webhook import router, setup as setup_webhook

__all__ = [
    "EvolutionClient",
    "EvolutionError",
    "SessionManager",
    "router",
    "setup_webhook",
]
