"""Tools para ler PDFs e gerar PDFs a partir de markdown."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError

# Limite de caracteres no retorno do PdfReadTool — evita explodir contexto.
_MAX_CHARS = 15000


def _parse_pages(spec: str | None, total: int) -> list[int]:
    """Converte 'N' ou 'A-B' (1-indexed) em lista de índices 0-based."""
    if spec is None:
        return list(range(total))
    spec = spec.strip()
    if "-" in spec:
        a, b = spec.split("-", 1)
        ini = max(1, int(a))
        fim = min(total, int(b))
        return list(range(ini - 1, fim))
    n = int(spec)
    if n < 1 or n > total:
        return []
    return [n - 1]


# ---------- Read ----------


class PdfReadArgs(BaseModel):
    path: str = Field(..., description="Caminho do PDF a ler.")
    pages: str | None = Field(
        None,
        description="Páginas a extrair: '1-5' para intervalo, '3' para uma única. None = todas.",
    )


class PdfReadTool(Tool):
    name = "pdf_read"
    destructive = False
    description = (
        "Extrai texto de um arquivo PDF. Pode ler todas as páginas ou um subconjunto. "
        "Retorno limitado a 15000 caracteres."
    )
    Args = PdfReadArgs

    def run(self, args: PdfReadArgs) -> str:  # type: ignore[override]
        # Import lazy do pypdf.
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError as exc:
            raise ToolError(
                "dependência ausente: instale `pypdf` (pip install pypdf)"
            ) from exc

        p = Path(args.path).expanduser()
        if not p.exists():
            raise ToolError(f"arquivo não encontrado: {p}")

        try:
            reader = PdfReader(str(p))
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"erro abrindo {p}: {exc}") from exc

        total = len(reader.pages)
        try:
            indices = _parse_pages(args.pages, total)
        except ValueError as exc:
            raise ToolError(f"spec de páginas inválido: {args.pages!r} ({exc})") from exc

        if not indices:
            return f"(sem páginas para extrair; total no doc: {total})"

        partes: list[str] = []
        for idx in indices:
            try:
                texto = reader.pages[idx].extract_text() or ""
            except Exception as exc:  # noqa: BLE001
                texto = f"(erro extraindo página {idx + 1}: {exc})"
            partes.append(f"--- página {idx + 1} ---\n{texto}")

        out = "\n\n".join(partes)
        if len(out) > _MAX_CHARS:
            out = out[:_MAX_CHARS] + f"\n\n... [truncado, {len(out) - _MAX_CHARS} chars omitidos]"
        return out


# ---------- Write (md → pdf) ----------


class PdfFromMarkdownArgs(BaseModel):
    path: str = Field(..., description="Caminho de saída do PDF.")
    markdown: str = Field(..., description="Conteúdo em markdown a converter para PDF.")


class PdfFromMarkdownTool(Tool):
    name = "pdf_write_md"
    destructive = True
    description = (
        "Gera um arquivo PDF a partir de conteúdo em markdown. "
        "Tenta usar weasyprint; se faltar, cai pra markdown+pdfkit."
    )
    Args = PdfFromMarkdownArgs

    def run(self, args: PdfFromMarkdownArgs) -> str:  # type: ignore[override]
        p = Path(args.path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)

        # Caminho 1: weasyprint (renderiza HTML→PDF nativamente).
        try:
            from weasyprint import HTML  # type: ignore
        except ImportError:
            HTML = None  # type: ignore

        # Sempre precisamos converter markdown→html. Lazy import.
        try:
            import markdown as _md  # type: ignore
        except ImportError as exc:
            raise ToolError(
                "dependência ausente: instale `markdown` (pip install markdown) "
                "e também `weasyprint` ou `pdfkit`."
            ) from exc

        html_body = _md.markdown(args.markdown, extensions=["tables", "fenced_code"])
        html_full = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<style>body{font-family:sans-serif;max-width:780px;margin:2em auto;}"
            "code{background:#f4f4f4;padding:2px 4px;border-radius:3px;}"
            "pre{background:#f4f4f4;padding:1em;border-radius:6px;overflow:auto;}"
            "table{border-collapse:collapse;}td,th{border:1px solid #ccc;padding:4px 8px;}"
            "</style></head><body>" + html_body + "</body></html>"
        )

        if HTML is not None:
            try:
                HTML(string=html_full).write_pdf(str(p))  # type: ignore[union-attr]
                return f"salvo em {p} (via weasyprint)"
            except Exception as exc:  # noqa: BLE001
                raise ToolError(f"erro gerando PDF com weasyprint: {exc}") from exc

        # Caminho 2: pdfkit (precisa do binário wkhtmltopdf no sistema).
        try:
            import pdfkit  # type: ignore
        except ImportError as exc:
            raise ToolError(
                "nenhum backend PDF disponível: instale `weasyprint` "
                "(pip install weasyprint) ou `pdfkit` + wkhtmltopdf."
            ) from exc

        try:
            pdfkit.from_string(html_full, str(p))
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"erro gerando PDF com pdfkit: {exc}") from exc
        return f"salvo em {p} (via pdfkit)"
