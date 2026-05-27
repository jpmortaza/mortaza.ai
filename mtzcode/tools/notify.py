"""NotifyTool — dispara notificação nativa do macOS via osascript."""
from __future__ import annotations

import subprocess

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError


class NotifyArgs(BaseModel):
    message: str = Field(..., description="Texto principal da notificação.")
    title: str = Field("mtzcode", description="Título da notificação.")
    subtitle: str | None = Field(None, description="Subtítulo opcional.")
    sound: str | None = Field(
        None,
        description="Nome do som (ex: 'default', 'Glass', 'Ping', 'Pop', 'Hero').",
    )


class NotifyTool(Tool):
    name = "macos_desktop_notification"
    destructive = False
    description = (
        "ATENÇÃO: dispara um POPUP NATIVO DO macOS (canto superior direito da tela), "
        "NÃO uma resposta no chat. NUNCA use isto pra responder ao usuário, dar "
        "feedback de progresso, ou comunicar resultado de tarefa — use texto comum. "
        "USO ÚNICO: alertar o usuário quando uma tarefa LONGA em background terminar "
        "e ele pode estar com a janela do mtzcode minimizada."
    )
    Args = NotifyArgs

    def run(self, args: NotifyArgs) -> str:  # type: ignore[override]
        # Monta o AppleScript escapando aspas e barras invertidas.
        message = _escape(args.message)
        title = _escape(args.title)
        script_parts = [f'display notification "{message}" with title "{title}"']
        if args.subtitle:
            script_parts.append(f'subtitle "{_escape(args.subtitle)}"')
        if args.sound:
            script_parts.append(f'sound name "{_escape(args.sound)}"')
        script = " ".join(script_parts)

        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise ToolError(
                "osascript não encontrado — esta tool só funciona no macOS."
            ) from exc
        except OSError as exc:
            raise ToolError(f"falha ao executar osascript: {exc}") from exc

        if result.returncode != 0:
            raise ToolError(
                f"falha ao exibir notificação (exit_code={result.returncode}): "
                f"{result.stderr.strip()}"
            )
        return f"notificação enviada: {args.title} — {args.message}"


def _escape(text: str) -> str:
    """Escapa aspas duplas e barras invertidas pra string literal AppleScript."""
    return text.replace("\\", "\\\\").replace('"', '\\"')
