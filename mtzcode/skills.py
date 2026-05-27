"""Sistema de skills do mtzcode.

Uma skill é um prompt especializado que redefine o comportamento do agent.
Cada skill é um diretório com um arquivo `SKILL.md`:

    skills/
      python-expert/
        SKILL.md

Formato do `SKILL.md`:

    ---
    name: python-expert
    description: Especialista em Python moderno (3.12+)
    tools: [read, write, edit, bash, grep, glob]   # opcional (default: todas)
    version: 1
    author: jpmortaza
    ---

    # Python Expert

    Você é um especialista em Python moderno...

Skills são carregadas de duas fontes (nessa ordem, a segunda sobrepõe a primeira):

1. **Oficiais** — `<repo>/skills/` (vindas junto com o mtzcode)
2. **Pessoais** — `~/.mtzcode/skills/` (criadas pelo usuário, ou via
   `mtzcode skills install <git-url>`)

No REPL, `/skill <nome>` ativa uma skill. `/skill` sem argumentos lista
as disponíveis. `/skill off` volta ao prompt padrão.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Skill:
    name: str                    # ID único (usado no comando /skill)
    description: str             # descrição curta mostrada na lista
    prompt: str                  # corpo markdown (sem o frontmatter)
    source_path: Path            # caminho do SKILL.md
    author: str = ""
    version: str = "1"
    tools: tuple[str, ...] = ()  # subset de tools permitidas (vazio = todas)
    source: str = "official"     # "official" ou "personal"


def _repo_skills_dir() -> Path:
    """Diretório de skills oficiais embarcadas no pacote."""
    # mtzcode/skills.py → repo_root/skills/
    return Path(__file__).parent.parent / "skills"


def _user_skills_dir() -> Path:
    """Diretório de skills pessoais do usuário."""
    custom = os.getenv("MTZCODE_SKILLS_DIR")
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".mtzcode" / "skills"


FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL
)


def _parse_frontmatter(text: str) -> tuple[dict[str, str | list[str]], str]:
    """Extrai o frontmatter YAML-ish e devolve (dict, corpo).

    Parser MUITO simples — não suporta YAML aninhado. Só o essencial:
    `key: value` e `key: [item1, item2]`. Chega pra nossos casos.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw_fm, body = m.group(1), m.group(2)
    data: dict[str, str | list[str]] = {}
    for line in raw_fm.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # Lista estilo [a, b, c]
        if value.startswith("[") and value.endswith("]"):
            items = [
                v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()
            ]
            data[key] = items
        else:
            # String (remove aspas se houver)
            data[key] = value.strip("\"'")
    return data, body.lstrip("\n")


def _load_skill_file(path: Path, source: str) -> Skill | None:
    """Carrega um SKILL.md. Retorna None em caso de erro."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm, body = _parse_frontmatter(text)

    name = str(fm.get("name") or path.parent.name).strip()
    if not name:
        return None
    description = str(fm.get("description") or "").strip()
    author = str(fm.get("author") or "").strip()
    version = str(fm.get("version") or "1").strip()
    tools_raw = fm.get("tools") or ()
    if isinstance(tools_raw, list):
        tools = tuple(t.strip() for t in tools_raw if t.strip())
    else:
        tools = ()

    return Skill(
        name=name,
        description=description,
        prompt=body.strip(),
        source_path=path,
        author=author,
        version=version,
        tools=tools,
        source=source,
    )


def load_skills() -> dict[str, Skill]:
    """Carrega todas as skills (oficiais + pessoais). Pessoais sobrepõem
    oficiais se tiverem o mesmo nome.
    """
    out: dict[str, Skill] = {}

    for src_dir, source_label in [
        (_repo_skills_dir(), "official"),
        (_user_skills_dir(), "personal"),
    ]:
        if not src_dir.exists() or not src_dir.is_dir():
            continue
        for skill_dir in sorted(src_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            skill = _load_skill_file(skill_file, source_label)
            if skill:
                out[skill.name] = skill

    return out


def skills_dirs() -> tuple[Path, Path]:
    """Retorna (oficial, pessoal) — usado pra msgs de ajuda."""
    return _repo_skills_dir(), _user_skills_dir()
