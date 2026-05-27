"""FindFilesTool — busca em todo o Mac via Spotlight (mdfind).

Habilidade "superpoderes" pra encontrar arquivos em qualquer lugar do
disco indexado pelo Spotlight, não só dentro do cwd. Útil quando o usuário
pede coisas como "ache fotos do Pedro" ou "encontre meu PDF do contrato"
sem dizer onde estão.

Usa `mdfind` (CLI nativo do macOS) que consulta o índice do Spotlight —
muito mais rápido que `find` recursivo.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError

MAX_RESULTS = 100

# Mapeia "kinds" amigáveis pra atributos Spotlight
_KIND_MAP: dict[str, str] = {
    "image":      "kMDItemContentTypeTree == 'public.image'",
    "photo":      "kMDItemContentTypeTree == 'public.image'",
    "video":      "kMDItemContentTypeTree == 'public.movie'",
    "audio":      "kMDItemContentTypeTree == 'public.audio'",
    "pdf":        "kMDItemContentType == 'com.adobe.pdf'",
    "doc":        "(kMDItemContentType == 'com.microsoft.word.doc' || kMDItemContentType == 'org.openxmlformats.wordprocessingml.document')",
    "spreadsheet":"(kMDItemContentType == 'com.microsoft.excel.xls' || kMDItemContentType == 'org.openxmlformats.spreadsheetml.sheet')",
    "code":       "kMDItemContentTypeTree == 'public.source-code'",
    "text":       "kMDItemContentTypeTree == 'public.text'",
    "folder":     "kMDItemContentType == 'public.folder'",
    "any":        "",
}


class FindFilesArgs(BaseModel):
    query: str = Field(
        ...,
        description="O que procurar. Pode ser parte do nome, palavra do conteúdo, ou frase. Ex: 'contrato', 'foto perfil', 'jean mortaza'.",
    )
    kind: str = Field(
        "any",
        description="Tipo de arquivo: image, photo, video, audio, pdf, doc, spreadsheet, code, text, folder, any.",
    )
    path: str | None = Field(
        None,
        description="Restringe a busca a esta pasta (e subpastas). Ex: '~/Pictures'. Omita pra buscar o disco todo.",
    )
    name_only: bool = Field(
        False,
        description="Se True, casa só pelo NOME do arquivo (não pelo conteúdo). Mais preciso pra nomes.",
    )


class FindFilesTool(Tool):
    name = "find_files"
    description = (
        "Busca arquivos em qualquer lugar do Mac via Spotlight (mdfind). "
        "Use quando o usuário pedir pra encontrar fotos, documentos, vídeos "
        "etc sem especificar onde estão. Filtra por tipo (image/pdf/video/...) "
        "e opcionalmente por pasta. MUITO mais rápido e amplo que `glob`."
    )
    Args = FindFilesArgs
    destructive = False

    def run(self, args: FindFilesArgs) -> str:  # type: ignore[override]
        if shutil.which("mdfind") is None:
            raise ToolError("mdfind não encontrado — esta habilidade requer macOS")

        # Monta a query Spotlight
        kind_filter = _KIND_MAP.get(args.kind.lower(), "")
        if args.name_only:
            text_part = f"kMDItemDisplayName == '*{args.query}*'cd"
        else:
            text_part = f"kMDItemTextContent == '*{args.query}*'cd || kMDItemDisplayName == '*{args.query}*'cd"

        if kind_filter:
            full_query = f"({text_part}) && ({kind_filter})"
        else:
            full_query = text_part

        cmd = ["mdfind", full_query]
        if args.path:
            onlyin = str(Path(args.path).expanduser())
            cmd = ["mdfind", "-onlyin", onlyin, full_query]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=20
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError("mdfind timeout (20s) — refine a query") from exc
        except OSError as exc:
            raise ToolError(f"erro ao rodar mdfind: {exc}") from exc

        if result.returncode != 0:
            raise ToolError(
                f"mdfind falhou: {result.stderr.strip() or 'erro desconhecido'}"
            )

        lines = [l for l in result.stdout.splitlines() if l.strip()]
        if not lines:
            scope = f" em {args.path}" if args.path else ""
            return f"(nenhum arquivo encontrado pra '{args.query}'{scope})"

        truncated = False
        if len(lines) > MAX_RESULTS:
            lines = lines[:MAX_RESULTS]
            truncated = True

        out = f"# {len(lines)} resultado(s) pra '{args.query}'\n" + "\n".join(lines)
        if truncated:
            out += f"\n... (truncado em {MAX_RESULTS})"
        return out
