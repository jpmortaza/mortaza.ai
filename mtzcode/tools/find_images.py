"""FindImagesTool — busca de imagens com filtros de metadado.

Especialização de find_files pra fotos. Permite filtrar por dimensão
mínima, data de captura, câmera, e ordenar por data. Útil quando o
usuário quer "uma foto minha de perfil em alta resolução" — sem precisar
saber onde está.

Para busca por **similaridade visual** (CLIP/embeddings) instale o opcional:
    pip install mtzcode[clip]
e use a habilidade `find_similar_image` (não implementada ainda — ver TODO).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError

MAX_RESULTS = 50


class FindImagesArgs(BaseModel):
    query: str = Field(
        "",
        description="Texto opcional pra filtrar por nome ou metadado (ex: 'perfil', 'praia', 'rosto'). Vazio = qualquer imagem.",
    )
    path: str | None = Field(
        None,
        description="Restringe a esta pasta. Default: ~/Pictures e ~/Desktop. Use '/' pra disco inteiro.",
    )
    min_width: int = Field(
        0, description="Largura mínima em pixels. 0 = sem filtro."
    )
    min_height: int = Field(
        0, description="Altura mínima em pixels. 0 = sem filtro."
    )
    sort: str = Field(
        "date_desc",
        description="Ordenação: date_desc (mais recente primeiro), date_asc, name.",
    )


class FindImagesTool(Tool):
    name = "find_images"
    description = (
        "Busca imagens/fotos no Mac via Spotlight, com filtros de dimensão "
        "mínima e ordenação por data. Use quando o usuário pedir uma foto "
        "específica ('foto minha em alta resolução', 'fotos de viagem 2023'). "
        "Retorna caminhos absolutos. Pra busca por similaridade visual use "
        "`find_similar_image` (requer extra clip)."
    )
    Args = FindImagesArgs
    destructive = False

    def run(self, args: FindImagesArgs) -> str:  # type: ignore[override]
        if shutil.which("mdfind") is None:
            raise ToolError("mdfind não encontrado — requer macOS")

        # Filtro base: imagens
        parts = ["kMDItemContentTypeTree == 'public.image'"]
        if args.query:
            parts.append(
                f"(kMDItemDisplayName == '*{args.query}*'cd "
                f"|| kMDItemTextContent == '*{args.query}*'cd "
                f"|| kMDItemKeywords == '*{args.query}*'cd)"
            )
        if args.min_width > 0:
            parts.append(f"kMDItemPixelWidth >= {args.min_width}")
        if args.min_height > 0:
            parts.append(f"kMDItemPixelHeight >= {args.min_height}")
        full_query = " && ".join(parts)

        # Escopo padrão: Pictures + Desktop (mais relevante que disco inteiro)
        if args.path is None:
            paths = [
                Path.home() / "Pictures",
                Path.home() / "Desktop",
                Path.home() / "Downloads",
            ]
            paths = [p for p in paths if p.exists()]
        else:
            paths = [Path(args.path).expanduser()]

        all_results: list[tuple[Path, float]] = []
        for p in paths:
            cmd = ["mdfind", "-onlyin", str(p), full_query]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=15
                )
            except subprocess.TimeoutExpired:
                continue
            except OSError as exc:
                raise ToolError(f"erro ao rodar mdfind: {exc}") from exc
            if result.returncode != 0:
                continue
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                fp = Path(line)
                try:
                    mtime = fp.stat().st_mtime
                except OSError:
                    mtime = 0.0
                all_results.append((fp, mtime))

        if not all_results:
            return f"(nenhuma imagem encontrada pra '{args.query or '*'}')"

        # Dedup por path
        seen: set[Path] = set()
        unique: list[tuple[Path, float]] = []
        for fp, mt in all_results:
            if fp in seen:
                continue
            seen.add(fp)
            unique.append((fp, mt))

        if args.sort == "date_desc":
            unique.sort(key=lambda x: -x[1])
        elif args.sort == "date_asc":
            unique.sort(key=lambda x: x[1])
        elif args.sort == "name":
            unique.sort(key=lambda x: x[0].name.lower())

        truncated = False
        if len(unique) > MAX_RESULTS:
            unique = unique[:MAX_RESULTS]
            truncated = True

        lines = [str(fp) for fp, _ in unique]
        out = f"# {len(lines)} imagem(ns) encontrada(s)\n" + "\n".join(lines)
        if truncated:
            out += f"\n... (truncado em {MAX_RESULTS})"
        return out
