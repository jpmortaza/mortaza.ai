"""Configurações padrão do mtzcode."""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from mtzcode.profiles import DEFAULT_PROFILE, Profile, get_profile


@dataclass(frozen=True)
class Config:
    profile: Profile
    system_prompt_path: Path
    request_timeout_s: float

    @classmethod
    def load(cls) -> "Config":
        prompt_path = Path(__file__).parent / "prompts" / "system.md"
        profile_name = os.getenv("MTZCODE_PROFILE", DEFAULT_PROFILE)
        return cls(
            profile=get_profile(profile_name),
            system_prompt_path=prompt_path,
            request_timeout_s=float(os.getenv("MTZCODE_TIMEOUT", "300")),
        )

    def with_profile(self, profile: Profile) -> "Config":
        return replace(self, profile=profile)

    def system_prompt(self) -> str:
        if self.system_prompt_path.exists():
            base = self.system_prompt_path.read_text(encoding="utf-8")
        else:
            base = (
                "Você é o mtzcode, um assistente de código útil. "
                "Responda em português."
            )
        # Concatena instruções pessoais/empresariais do usuário (vindas do
        # painel de Configurações na web UI). Vai como bloco extra no fim
        # do system prompt pra não conflitar com regras de tool calling.
        try:
            from mtzcode.settings import get_settings
            extra = (get_settings().personal_context or "").strip()
            if extra:
                base = (
                    f"{base}\n\n# Contexto pessoal do usuário\n{extra}\n"
                )
        except Exception:
            pass
        return base
