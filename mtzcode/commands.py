"""Slash commands customizados do mtzcode.

Procura arquivos `*.md` em `~/.mtzcode/commands/` e transforma cada um em
um comando de REPL. O nome do arquivo (sem `.md`) vira o comando: `review.md`
→ `/review`.

O conteúdo do arquivo é usado como template de prompt. Suporta substituição
simples de `$ARGUMENTS` pelo texto que o usuário digitar depois do comando.

Exemplo — `~/.mtzcode/commands/review.md`:

    Faça uma revisão crítica do código em $ARGUMENTS. Aponte bugs potenciais,
    problemas de segurança, e sugestões de melhoria. Seja direto e conciso.

Uso no REPL:

    você › /review mtzcode/agent.py
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SlashCommand:
    name: str          # sem a barra inicial
    template: str      # conteúdo do arquivo .md
    source_path: Path  # caminho do arquivo

    def render(self, arguments: str) -> str:
        """Expande a template substituindo $ARGUMENTS."""
        return self.template.replace("$ARGUMENTS", arguments).strip()


def commands_dir() -> Path:
    """Diretório onde o usuário coloca slash commands customizados."""
    custom = os.getenv("MTZCODE_COMMANDS_DIR")
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".mtzcode" / "commands"


def load_commands() -> dict[str, SlashCommand]:
    """Carrega todos os `*.md` do diretório de commands. Tolerante a erros:
    arquivos ilegíveis são silenciosamente ignorados."""
    out: dict[str, SlashCommand] = {}
    d = commands_dir()
    if not d.exists() or not d.is_dir():
        return out
    for f in sorted(d.glob("*.md")):
        name = f.stem.strip()
        if not name:
            continue
        try:
            template = f.read_text(encoding="utf-8")
        except OSError:
            continue
        out[name] = SlashCommand(name=name, template=template, source_path=f)
    return out


def parse_slash(user_input: str) -> tuple[str, str] | None:
    """Se `user_input` começa com `/nome`, retorna (nome, resto_dos_args).
    Caso contrário, None. O nome é sempre sem a barra."""
    if not user_input.startswith("/"):
        return None
    rest = user_input[1:].strip()
    if not rest:
        return None
    parts = rest.split(None, 1)
    name = parts[0]
    args = parts[1] if len(parts) > 1 else ""
    return name, args
