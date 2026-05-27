"""TextWriterTool — assistente para redigir textos longos em arquivos."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError


class TextWriterArgs(BaseModel):
    path: str = Field(..., description="Caminho do arquivo de texto.")
    content: str = Field(..., description="Conteúdo a escrever (texto longo).")
    mode: Literal["create", "append", "replace_section"] = Field(
        "create",
        description=(
            "create = cria/sobrescreve o arquivo; "
            "append = adiciona ao final; "
            "replace_section = substitui o trecho entre `section_marker` e o próximo marker."
        ),
    )
    section_marker: str | None = Field(
        None,
        description="Marcador da seção a substituir, ex: '<!-- intro -->'. Obrigatório no modo replace_section.",
    )


class TextWriterTool(Tool):
    name = "text_writer"
    destructive = True
    description = (
        "Ferramenta pra ajudar o usuário a escrever textos longos (artigos, emails, posts, "
        "documentos). Salva em arquivo e suporta continuação/revisão. Diferente de 'write' "
        "que é pra código."
    )
    Args = TextWriterArgs

    def run(self, args: TextWriterArgs) -> str:  # type: ignore[override]
        p = Path(args.path).expanduser()
        # Garante que o diretório pai existe (mkdir -p).
        p.parent.mkdir(parents=True, exist_ok=True)

        n_chars = 0

        if args.mode == "create":
            # Sobrescreve / cria do zero.
            try:
                p.write_text(args.content, encoding="utf-8")
            except OSError as exc:
                raise ToolError(f"erro escrevendo {p}: {exc}") from exc
            n_chars = len(args.content)

        elif args.mode == "append":
            # Adiciona ao final, criando se não existir.
            try:
                with p.open("a", encoding="utf-8") as f:
                    n_chars = f.write(args.content)
            except OSError as exc:
                raise ToolError(f"erro adicionando em {p}: {exc}") from exc

        elif args.mode == "replace_section":
            if not args.section_marker:
                raise ToolError("modo replace_section exige `section_marker`.")
            if not p.exists():
                raise ToolError(f"arquivo não existe: {p} (replace_section precisa de arquivo existente)")

            try:
                texto = p.read_text(encoding="utf-8")
            except OSError as exc:
                raise ToolError(f"erro lendo {p}: {exc}") from exc

            marker = args.section_marker
            ini = texto.find(marker)
            if ini == -1:
                raise ToolError(f"marker `{marker}` não encontrado em {p}")

            # Início do bloco = logo após o marker.
            bloco_ini = ini + len(marker)
            # Procura o próximo marker (qualquer outro `<!-- xxx -->` ou repetição do mesmo).
            # Heurística: tenta primeiro outro marker no estilo HTML comment; se não, o mesmo.
            prox = -1
            # Se o marker é um comentário HTML, procura o próximo "<!--" depois do bloco.
            if marker.startswith("<!--"):
                prox = texto.find("<!--", bloco_ini)
            if prox == -1:
                # Fallback: procura repetição do mesmo marker.
                prox = texto.find(marker, bloco_ini)
            if prox == -1:
                # Sem próximo marker → substitui até o fim do arquivo.
                prox = len(texto)

            novo = texto[:bloco_ini] + "\n" + args.content + "\n" + texto[prox:]
            try:
                p.write_text(novo, encoding="utf-8")
            except OSError as exc:
                raise ToolError(f"erro escrevendo {p}: {exc}") from exc
            n_chars = len(args.content)

        else:  # pragma: no cover — pydantic já valida o Literal
            raise ToolError(f"mode inválido: {args.mode}")

        return f"{p} ({n_chars} chars escritos, mode={args.mode})"
