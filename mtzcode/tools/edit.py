"""EditTool — substituição exata de string em um arquivo (estilo Claude Code)."""
from __future__ import annotations

import difflib
from pathlib import Path

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError


class EditArgs(BaseModel):
    path: str = Field(..., description="Caminho do arquivo a editar.")
    old_string: str = Field(
        ...,
        description=(
            "Texto exato a ser substituído. Inclua contexto suficiente "
            "para que apareça apenas uma vez no arquivo (a menos que use replace_all)."
        ),
    )
    new_string: str = Field(..., description="Texto que vai substituir old_string.")
    replace_all: bool = Field(
        False, description="Se true, substitui todas as ocorrências."
    )


class EditTool(Tool):
    name = "edit"
    destructive = True
    description = (
        "Substitui um trecho exato de texto em um arquivo. "
        "Use para mudanças localizadas — leia o arquivo antes para garantir "
        "que old_string bate exatamente (incluindo espaços/indentação). "
        "Falha se old_string não for único, a menos que replace_all=true."
    )
    Args = EditArgs

    def run(self, args: EditArgs) -> str:  # type: ignore[override]
        p = Path(args.path).expanduser()
        if not p.exists():
            raise ToolError(f"arquivo não existe: {p}")

        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"erro lendo {p}: {exc}") from exc

        if args.old_string == args.new_string:
            raise ToolError("old_string e new_string são iguais — nada a fazer.")

        count = text.count(args.old_string)
        if count == 0:
            raise ToolError(
                "old_string não encontrado no arquivo. "
                "Leia o arquivo de novo para garantir que o texto bate exatamente."
            )
        if count > 1 and not args.replace_all:
            raise ToolError(
                f"old_string aparece {count} vezes. "
                "Inclua mais contexto para deixar único, ou passe replace_all=true."
            )

        if args.replace_all:
            new_text = text.replace(args.old_string, args.new_string)
            applied = count
        else:
            new_text = text.replace(args.old_string, args.new_string, 1)
            applied = 1

        try:
            p.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"erro escrevendo {p}: {exc}") from exc

        # Gera diff unified pro resultado — a CLI detecta e renderiza colorido
        diff = difflib.unified_diff(
            text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{p.name}",
            tofile=f"b/{p.name}",
            n=3,
        )
        diff_text = "".join(diff)
        header = f"editado: {p} ({applied} substituição{'ões' if applied > 1 else ''})\n"
        if diff_text:
            return header + "\n" + diff_text
        return header