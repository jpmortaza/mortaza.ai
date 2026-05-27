"""Perfis de modelo do mtzcode.

Cada perfil descreve um backend (Ollama local ou serviço cloud OpenAI-compatible)
com seu modelo, endpoint e variável de API key. Permite trocar de modelo no
REPL com `/modelo` sem reiniciar.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    name: str            # ID curto, usado em comandos
    label: str           # Nome bonito mostrado pro usuário
    backend: str         # "ollama" ou "groq"
    model: str           # ID do modelo enviado pro backend
    base_url: str        # URL OpenAI-compatible (sem /chat/completions)
    api_key_env: str | None  # nome da env var com a key (None = sem auth)
    is_local: bool       # True = roda na máquina, False = chama serviço externo
    description: str

    @property
    def needs_api_key(self) -> bool:
        return self.api_key_env is not None


PROFILES: dict[str, Profile] = {
    "qwen-14b": Profile(
        name="qwen-14b",
        label="Qwen 2.5 Coder 14B (local)",
        backend="ollama",
        model="qwen2.5-coder:14b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
        is_local=True,
        description="Modelo grande local. Melhor qualidade. ~25 tok/s no M4. Cold start ~30s.",
    ),
    "qwen-7b": Profile(
        name="qwen-7b",
        label="Qwen 2.5 Coder 7B (local, leve)",
        backend="ollama",
        model="qwen2.5-coder:7b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
        is_local=True,
        description="Modelo leve local. Mais rápido (~50 tok/s), qualidade boa pra tarefas simples.",
    ),
    "groq-llama": Profile(
        name="groq-llama",
        label="Llama 3.3 70B (Groq cloud)",
        backend="groq",
        model="llama-3.3-70b-versatile",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        is_local=False,
        description="Cloud, super rápido (~300 tok/s). ATENÇÃO: dados saem da máquina.",
    ),
    "sabia-3": Profile(
        name="sabia-3",
        label="Sabiá 3 (Maritaca, PT-BR nativo)",
        backend="maritaca",
        model="sabia-3",
        base_url="https://chat.maritaca.ai/api",
        api_key_env="MARITACA_API_KEY",
        is_local=False,
        description="Sabiá 3 da Maritaca AI — treinado em PT-BR nativo, melhor qualidade pra português. Cloud, dados saem da máquina.",
    ),
    "qwen-instruct-14b": Profile(
        name="qwen-instruct-14b",
        label="Qwen 2.5 14B Instruct (local, PT-BR)",
        backend="ollama",
        model="qwen2.5:14b-instruct",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
        is_local=True,
        description="Qwen 2.5 14B — versão instruct (não coder) tem PT melhor. Bom equilíbrio qualidade/velocidade local.",
    ),
    "gemma2-9b": Profile(
        name="gemma2-9b",
        label="Gemma 2 9B (local)",
        backend="ollama",
        model="gemma2:9b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
        is_local=True,
        description="Google Gemma 2 9B — forte em PT-BR multilíngue. Roda local via Ollama.",
    ),
    "llama3.1-8b": Profile(
        name="llama3.1-8b",
        label="Llama 3.1 8B (local)",
        backend="ollama",
        model="llama3.1:8b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
        is_local=True,
        description="Llama 3.1 8B — bom balanço PT/EN. Leve e rápido pra rodar local.",
    ),
    # NOTE: removidos deepseek-coder-v2 e deepseek-r1 — nenhum dos dois
    # suporta tool calling no Ollama (a API devolve 400 "does not support tools").
    # O mtzcode é um agent loop que depende de tools pra fazer qualquer
    # coisa útil, então esses profiles ficavam inutilizáveis. Quando o
    # Ollama lançar uma versão do DeepSeek com tools, voltamos a expor.
    "mtzcode-pt": Profile(
        name="mtzcode-pt",
        label="mtzcode PT-BR (fine-tuned)",
        backend="ollama",
        model="mtzcode-pt:latest",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
        is_local=True,
        description="Perfil reservado pro modelo próprio fine-tuned em PT-BR (criar com `mtzcode finetune`).",
    ),
    "claude-sonnet": Profile(
        name="claude-sonnet",
        label="Claude Sonnet 4.6 (Anthropic)",
        backend="anthropic",
        model="claude-sonnet-4-6",
        base_url="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        is_local=False,
        description="Claude Sonnet 4.6 — qualidade alta, custo médio. Cloud, dados saem da máquina.",
    ),
    "claude-haiku": Profile(
        name="claude-haiku",
        label="Claude Haiku 4.5 (Anthropic)",
        backend="anthropic",
        model="claude-haiku-4-5-20251001",
        base_url="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        is_local=False,
        description="Claude Haiku 4.5 — rápido e barato, bom pra WhatsApp. Cloud.",
    ),
    "claude-opus": Profile(
        name="claude-opus",
        label="Claude Opus 4.7 (Anthropic)",
        backend="anthropic",
        model="claude-opus-4-7",
        base_url="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        is_local=False,
        description="Claude Opus 4.7 — melhor qualidade, custo alto. Cloud.",
    ),
}

DEFAULT_PROFILE = "qwen-14b"


def get_profile(name: str) -> Profile:
    if name not in PROFILES:
        raise KeyError(
            f"perfil desconhecido: {name}. Disponíveis: {', '.join(PROFILES)}"
        )
    return PROFILES[name]


def list_profiles() -> list[Profile]:
    return list(PROFILES.values())
