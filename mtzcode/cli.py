"""CLI do mtzcode — REPL com agent loop, streaming, diffs e confirmação destrutiva."""
from __future__ import annotations

import sys
from typing import Any

import typer
from rich.align import Align
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from pathlib import Path

from mtzcode import __version__
from mtzcode.agent import Agent, AgentEvent
from mtzcode.client import ChatClient, ChatClientError, make_client
from mtzcode.commands import SlashCommand, commands_dir, load_commands, parse_slash
from mtzcode.config import Config
from mtzcode.profiles import PROFILES, Profile, get_profile, list_profiles
from mtzcode.session_log import (
    SessionLogger,
    events_to_history,
    latest_session_for_cwd,
    load_session,
    make_event_callback,
)
from mtzcode.skills import Skill, load_skills, skills_dirs
from mtzcode.tools import default_registry

_PLAN_MODE_PROMPT_PATH = Path(__file__).parent / "prompts" / "plan_mode.md"

app = typer.Typer(
    add_completion=False,
    help="mtzcode — assistente de código local (Ollama), 100% offline.",
)
knowledge_app = typer.Typer(
    add_completion=False,
    help="Gerenciar knowledge bases (memória permanente de documentos).",
)
app.add_typer(knowledge_app, name="knowledge", help="Knowledge bases (docs permanentes).")
console = Console()


_BANNER_ASCII = r"""
███╗   ███╗████████╗███████╗ ██████╗ ██████╗ ██████╗ ███████╗
████╗ ████║╚══██╔══╝╚══███╔╝██╔════╝██╔═══██╗██╔══██╗██╔════╝
██╔████╔██║   ██║     ███╔╝ ██║     ██║   ██║██║  ██║█████╗
██║╚██╔╝██║   ██║    ███╔╝  ██║     ██║   ██║██║  ██║██╔══╝
██║ ╚═╝ ██║   ██║   ███████╗╚██████╗╚██████╔╝██████╔╝███████╗
╚═╝     ╚═╝   ╚═╝   ╚══════╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝
"""


def _banner(cfg: Config, n_tools: int) -> None:
    logo = Text(_BANNER_ASCII, style="bold bright_cyan")
    console.print(Align.center(logo))
    signature = Text("by Jean Mortaza", style="italic bright_magenta")
    console.print(Align.center(signature))
    console.print()

    profile = cfg.profile
    locality = "[green]local[/]" if profile.is_local else "[red]cloud[/]"
    info = (
        f"[dim]versão[/] [bold]{__version__}[/]   "
        f"[dim]perfil[/] [yellow]{profile.name}[/] ({locality})   "
        f"[dim]tools[/] [green]{n_tools}[/]\n"
        f"[dim]modelo[/] {profile.model}\n"
        f"[dim]host[/]   {profile.base_url}\n\n"
        f"[dim]Comandos: /sair  /limpar  /modelo  /ajuda[/]"
    )
    console.print(Align.center(Panel.fit(info, border_style="cyan")))
    console.print()


# ---------------------------------------------------------------------------
# Session state — flags compartilhadas entre REPL e callbacks
# ---------------------------------------------------------------------------
class SessionState:
    def __init__(self) -> None:
        # Tools marcadas como "sempre permitir" nesta sessão
        self.always_allow: set[str] = set()
        # Plan mode: se True, bloqueia tools destrutivas independente do always_allow
        self.plan_mode: bool = False
        # Skill ativa: se set, o system prompt atual vem dela
        self.active_skill: Skill | None = None

    def should_confirm(self, tool_name: str) -> bool:
        if self.plan_mode:
            return True  # sempre confirma em plan mode (será recusado automaticamente)
        return tool_name not in self.always_allow


def _make_confirm_cb(state: SessionState):
    """Retorna um confirm_cb que pergunta [s/N/sempre] e respeita 'sempre'.
    Em plan_mode, recusa automaticamente tools destrutivas."""

    def confirm(tool_name: str, args: dict) -> bool:
        if state.plan_mode:
            console.print(
                f"[yellow]⊘ plano ativo:[/] [bold]{tool_name}[/] bloqueada. "
                "Digite /executar pra sair do modo plano."
            )
            return False
        if not state.should_confirm(tool_name):
            return True
        args_preview = _preview_args(args)
        console.print(
            f"\n[yellow]⚠  confirmação:[/] executar [bold cyan]{tool_name}[/] "
            f"[dim]{args_preview}[/]?"
        )
        try:
            resp = (
                console.input(
                    "[bold]› [/][dim]([/][green]s[/]im / [red]N[/]ão / "
                    "[cyan]a[/]lways[dim])[/] "
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            console.print("[dim](recusado)[/]")
            return False
        if resp in {"s", "sim", "y", "yes"}:
            return True
        if resp in {"a", "always", "sempre"}:
            state.always_allow.add(tool_name)
            console.print(
                f"[dim](ok — {tool_name} liberada pro resto da sessão)[/]"
            )
            return True
        return False

    return confirm


# ---------------------------------------------------------------------------
# Event rendering
# ---------------------------------------------------------------------------
class EventRenderer:
    """Renderiza eventos do agent no terminal. Mantém estado pra streaming."""

    def __init__(self) -> None:
        self._streaming_text = False

    def handle(self, ev: AgentEvent) -> None:
        if ev.kind == "text_delta":
            if not self._streaming_text:
                console.print("[dim]mtzcode ›[/] ", end="")
                self._streaming_text = True
            console.print(
                ev.data["delta"], end="", markup=False, highlight=False
            )
        elif ev.kind == "assistant_text_end":
            if self._streaming_text:
                console.print()  # newline pra fechar a linha de streaming
                self._streaming_text = False
        elif ev.kind == "assistant_text":
            # Modo não-streaming — renderiza tudo de uma vez em markdown
            console.print(
                Panel(
                    Markdown(ev.data["text"]),
                    border_style="green",
                    title="mtzcode",
                )
            )
        elif ev.kind == "tool_call":
            self._close_streaming()
            args_preview = _preview_args(ev.data["args"])
            console.print(
                f"[dim cyan]→ tool[/] [bold cyan]{ev.data['name']}[/] "
                f"[dim]{args_preview}[/]"
            )
        elif ev.kind == "tool_result":
            self._render_tool_result(ev.data["name"], ev.data["result"])
        elif ev.kind == "tool_error":
            self._close_streaming()
            console.print(f"[red]✖ {ev.data['name']}:[/] {ev.data['error']}")
        elif ev.kind == "tool_denied":
            self._close_streaming()
            console.print(f"[yellow]⊘ {ev.data['name']} recusada pelo usuário[/]")
        elif ev.kind == "max_iterations":
            self._close_streaming()
            console.print(
                f"[yellow]⚠ atingiu limite de {ev.data['limit']} iterações[/]"
            )

    def finalize(self, text: str) -> None:
        if self._streaming_text:
            console.print()  # fecha a linha
            self._streaming_text = False
        # Não re-renderiza o texto final — já foi streamado

    def _close_streaming(self) -> None:
        if self._streaming_text:
            console.print()
            self._streaming_text = False

    def _render_tool_result(self, name: str, result: str) -> None:
        # Se o resultado contém um diff unified, renderiza com syntax highlight
        if "--- a/" in result and "+++ b/" in result:
            header, _, diff_body = result.partition("--- a/")
            diff_body = "--- a/" + diff_body
            if header.strip():
                console.print(f"[dim]{header.strip()}[/]")
            console.print(
                Panel(
                    Syntax(
                        diff_body,
                        "diff",
                        theme="ansi_dark",
                        background_color="default",
                        word_wrap=True,
                    ),
                    title=f"resultado: {name}",
                    border_style="dim",
                    title_align="left",
                )
            )
            return

        preview = (
            result if len(result) < 800 else result[:800] + "\n... (truncado)"
        )
        console.print(
            Panel(
                preview,
                title=f"resultado: {name}",
                border_style="dim",
                title_align="left",
            )
        )


def _preview_args(args: dict) -> str:
    if not args:
        return "()"
    parts = []
    for k, v in args.items():
        sv = repr(v)
        if len(sv) > 60:
            sv = sv[:57] + "..."
        parts.append(f"{k}={sv}")
    return "(" + ", ".join(parts) + ")"


# ---------------------------------------------------------------------------
# Comandos do REPL
# ---------------------------------------------------------------------------
def _show_profiles_menu(current: Profile) -> None:
    console.print()
    console.print("[bold]Perfis disponíveis:[/]")
    for i, p in enumerate(list_profiles(), start=1):
        marker = "[green]●[/]" if p.name == current.name else " "
        locality = "[green]local[/]" if p.is_local else "[red]cloud[/]"
        console.print(
            f"  {marker} [bold cyan]{i}[/]. [bold]{p.label}[/] ({locality})\n"
            f"      [dim]{p.description}[/]"
        )
    console.print()


def _switch_profile(current: Profile) -> Profile | None:
    _show_profiles_menu(current)
    try:
        choice = console.input(
            "[bold blue]escolha um perfil (número, enter pra cancelar) ›[/] "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not choice or not choice.isdigit():
        return None
    idx = int(choice) - 1
    profiles = list_profiles()
    if idx < 0 or idx >= len(profiles):
        console.print("[red]número fora do intervalo[/]")
        return None
    new_profile = profiles[idx]
    if new_profile.name == current.name:
        console.print("[dim]já está nesse perfil.[/]")
        return None
    if not new_profile.is_local:
        console.print(
            f"[yellow]⚠  Atenção:[/] {new_profile.label} envia seus dados "
            "pra um servidor externo. Quebra a garantia de privacidade local."
        )
    return new_profile


def _repl(cfg: Config, resume: bool = False, ask_resume: bool = True) -> None:
    registry = default_registry()
    _banner(cfg, len(registry))

    try:
        client = make_client(cfg.profile, cfg.request_timeout_s)
    except ChatClientError as exc:
        console.print(f"[red]erro ao iniciar cliente:[/] {exc}")
        return

    state = SessionState()
    confirm_cb = _make_confirm_cb(state)
    default_prompt = cfg.system_prompt()
    agent = Agent(client, registry, default_prompt, confirm_cb=confirm_cb)
    renderer = EventRenderer()

    # ------------------------------------------------------------------
    # Session log + auto-resume
    # ------------------------------------------------------------------
    cwd_now = str(Path.cwd().resolve())
    session_logger = SessionLogger()
    session_logger.log_meta("cwd", cwd_now)
    session_logger.log_meta("profile", cfg.profile.name)
    session_logger.log_meta("model", cfg.profile.model)

    # Tenta achar a sessão anterior deste cwd e oferecer retomar
    prev = latest_session_for_cwd(cwd_now)
    # Ignora a sessão que acabou de ser criada (mesmo path)
    if prev and prev.get("path") == str(session_logger.path):
        prev = None

    should_resume = False
    if prev:
        if resume:
            should_resume = True
        elif ask_resume:
            preview = (prev.get("first_user_message") or "").strip()
            if len(preview) > 60:
                preview = preview[:57] + "..."
            console.print(
                f"[dim]última sessão neste cwd:[/] [cyan]{prev.get('ts')}[/]"
                + (f" — [italic]{preview}[/]" if preview else "")
            )
            try:
                ans = console.input("[bold]retomar? [s/N] [/]").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            should_resume = ans in {"s", "sim", "y", "yes"}

    if should_resume and prev:
        try:
            events = load_session(Path(prev["path"]))
            history = events_to_history(events)
            agent.history = [{"role": "system", "content": default_prompt}] + history
            n_user = sum(1 for h in history if h.get("role") == "user")
            console.print(
                f"[green]sessão retomada[/] — {len(history)} mensagens "
                f"({n_user} do usuário)\n"
            )
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]falha ao retomar sessão:[/] {exc}")

    # Carrega slash commands customizados do usuário
    custom_commands = load_commands()
    if custom_commands:
        console.print(
            f"[dim]slash commands customizados carregados: "
            f"{', '.join('/' + n for n in custom_commands)}[/]"
        )

    # Carrega skills (oficiais + pessoais)
    skills = load_skills()
    if skills:
        console.print(
            f"[dim]{len(skills)} skills disponíveis — /skill pra listar[/]"
        )

    try:
        while True:
            try:
                user_input = console.input("[bold blue]você ›[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]até mais.[/]")
                return

            if not user_input:
                continue

            # --- comandos built-in ---
            if user_input in {"/sair", "/exit", "/quit"}:
                console.print("[dim]até mais.[/]")
                return
            if user_input == "/limpar":
                agent.reset()
                agent.set_system_prompt(default_prompt)
                state.always_allow.clear()
                state.plan_mode = False
                console.print("[dim]histórico limpo.[/]")
                continue
            if user_input == "/ajuda":
                _print_help(registry, custom_commands)
                continue
            if user_input in {"/modelo", "/model"}:
                new_profile = _switch_profile(client.profile)
                if new_profile is None:
                    continue
                try:
                    new_client = make_client(new_profile, cfg.request_timeout_s)
                except ChatClientError as exc:
                    console.print(f"[red]falha ao trocar:[/] {exc}")
                    continue
                client.close()
                client = new_client
                agent.client = new_client
                console.print(
                    f"[green]✓[/] agora usando [bold]{new_profile.label}[/] "
                    f"[dim](histórico preservado)[/]"
                )
                continue
            if user_input in {"/plano", "/plan"}:
                if state.plan_mode:
                    console.print("[dim]já está em modo plano.[/]")
                    continue
                try:
                    plan_prompt = _PLAN_MODE_PROMPT_PATH.read_text(encoding="utf-8")
                except OSError:
                    plan_prompt = (
                        "Você está em modo de planejamento: use apenas tools "
                        "read-only. Entregue um plano estruturado ao final."
                    )
                agent.set_system_prompt(plan_prompt)
                state.plan_mode = True
                console.print(
                    "[yellow]◐ modo plano ativado[/] — tools destrutivas "
                    "bloqueadas. Digite [cyan]/executar[/] pra sair."
                )
                continue
            if user_input in {"/executar", "/exec", "/run"}:
                if not state.plan_mode:
                    console.print("[dim]não estava em modo plano.[/]")
                    continue
                agent.set_system_prompt(default_prompt)
                state.plan_mode = False
                console.print(
                    "[green]▶ modo plano desativado[/] — tools destrutivas "
                    "liberadas novamente (ainda pedem confirmação)."
                )
                continue
            if user_input in {"/retomar", "/resume"}:
                from mtzcode.session_log import list_sessions
                items = list_sessions()
                if not items:
                    console.print("[dim]nenhuma sessão salva ainda.[/]")
                    continue
                console.print("[bold]sessões recentes:[/]")
                for i, s in enumerate(items[:10], start=1):
                    preview = (s.get("first_user_message") or "").strip()
                    if len(preview) > 60:
                        preview = preview[:57] + "..."
                    console.print(
                        f"  [cyan]{i}[/]. {s.get('ts')}"
                        + (f" — [italic dim]{preview}[/]" if preview else "")
                    )
                try:
                    pick = console.input("[bold]número (ou Enter pra cancelar): [/]").strip()
                except (EOFError, KeyboardInterrupt):
                    continue
                if not pick:
                    continue
                try:
                    idx = int(pick) - 1
                    target = items[idx]
                except (ValueError, IndexError):
                    console.print("[red]escolha inválida.[/]")
                    continue
                try:
                    events = load_session(Path(target["path"]))
                    history = events_to_history(events)
                    agent.history = [{"role": "system", "content": default_prompt}] + history
                    n_user = sum(1 for h in history if h.get("role") == "user")
                    console.print(
                        f"[green]sessão retomada[/] — {len(history)} mensagens "
                        f"({n_user} do usuário)"
                    )
                except Exception as exc:  # noqa: BLE001
                    console.print(f"[red]falha:[/] {exc}")
                continue
            if user_input in {"/indexar", "/index"}:
                try:
                    _index_cwd()
                except Exception as exc:  # noqa: BLE001
                    console.print(f"[red]erro indexando:[/] {exc}")
                continue
            if user_input.startswith("/skill"):
                parts = user_input.split(None, 1)
                arg = parts[1].strip() if len(parts) > 1 else ""
                if not arg:
                    _print_skills(skills, state.active_skill)
                    continue
                if arg == "off":
                    if state.active_skill is None:
                        console.print("[dim]nenhuma skill ativa.[/]")
                        continue
                    agent.set_system_prompt(default_prompt)
                    state.active_skill = None
                    console.print("[green]✓[/] skill desativada")
                    continue
                skill = skills.get(arg)
                if skill is None:
                    console.print(
                        f"[red]skill desconhecida:[/] {arg}. "
                        "Digite [cyan]/skill[/] pra listar."
                    )
                    continue
                agent.set_system_prompt(skill.prompt)
                state.active_skill = skill
                console.print(
                    f"[green]✓[/] skill ativada: [bold]{skill.name}[/] "
                    f"[dim]({skill.source})[/]"
                )
                continue

            # --- slash commands customizados ---
            parsed = parse_slash(user_input)
            if parsed is not None:
                cmd_name, cmd_args = parsed
                if cmd_name in custom_commands:
                    rendered = custom_commands[cmd_name].render(cmd_args)
                    console.print(
                        f"[dim]→ expandindo /{cmd_name} ({len(rendered)} chars)[/]"
                    )
                    user_input = rendered
                else:
                    # Slash desconhecido — não repassa pro modelo, evita confusão
                    console.print(
                        f"[red]comando desconhecido:[/] /{cmd_name}. "
                        "Digite [cyan]/ajuda[/] pra ver os disponíveis."
                    )
                    continue

            # --- turno do agent ---
            try:
                session_logger.log_user(user_input)
            except Exception:
                pass

            on_event = make_event_callback(session_logger, renderer.handle)

            try:
                final_text = agent.run_streaming(
                    user_input, on_event=on_event
                )
                renderer.finalize(final_text)
            except ChatClientError as exc:
                console.print(f"\n[red]erro:[/] {exc}")
                continue
            except KeyboardInterrupt:
                console.print("\n[yellow]interrompido.[/]")
                continue
    finally:
        client.close()
        try:
            session_logger.close()
        except Exception:
            pass


def _index_cwd() -> None:
    """Indexa o cwd atual — usado pelo /indexar do REPL."""
    from mtzcode.rag import EmbeddingClient, Index, ProjectIndexer

    root = Path.cwd()
    db_path = root / ".mtzcode" / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    console.print(f"[dim]indexando {root}[/]")

    embedder = EmbeddingClient()
    idx = Index(db_path)
    indexer = ProjectIndexer(root, idx, embedder)
    try:
        with console.status("[dim]indexando...[/]", spinner="dots") as status:
            def on_progress(p, rel):
                status.update(
                    f"[dim]{p.files_indexed} arquivos / {p.chunks_created} chunks — {rel}[/]"
                )
            progress = indexer.index_project(on_progress=on_progress)
    finally:
        idx.close()
        embedder.close()

    console.print(
        f"[green]✓[/] {progress.files_indexed} arquivos, "
        f"{progress.chunks_created} chunks"
    )


def _print_skills(skills: dict[str, Skill], active: Skill | None) -> None:
    if not skills:
        official, personal = skills_dirs()
        console.print(
            "[dim]nenhuma skill encontrada. Coloque skills em:\n"
            f"  oficial: {official}\n"
            f"  pessoal: {personal}[/]"
        )
        return
    console.print("[bold]Skills disponíveis:[/]")
    for name, s in skills.items():
        marker = "[green]●[/]" if active and active.name == name else " "
        badge = "[dim](oficial)[/]" if s.source == "official" else "[cyan](pessoal)[/]"
        console.print(f"  {marker} [bold cyan]{name}[/] {badge}")
        if s.description:
            console.print(f"      [dim]{s.description}[/]")
    console.print(
        "\n[dim]Uso: [cyan]/skill <nome>[/] · "
        "desativar: [cyan]/skill off[/][/]"
    )


def _print_help(registry, custom_commands: dict[str, SlashCommand] | None = None) -> None:
    console.print(
        "[bold]Comandos do REPL:[/]\n"
        "  [cyan]/sair[/]       encerra a sessão\n"
        "  [cyan]/limpar[/]     zera o histórico da conversa\n"
        "  [cyan]/modelo[/]     troca de modelo (menu interativo)\n"
        "  [cyan]/plano[/]      ativa modo de planejamento (sem tools destrutivas)\n"
        "  [cyan]/executar[/]   sai do modo plano\n"
        "  [cyan]/indexar[/]    gera embeddings do projeto pra search_code\n"
        "  [cyan]/retomar[/]    lista sessões anteriores e retoma uma\n"
        "  [cyan]/skill[/]      lista ou ativa skills (ex: /skill python-expert)\n"
        "  [cyan]/ajuda[/]      mostra esta mensagem\n"
    )
    if custom_commands:
        console.print("[bold]Slash commands customizados:[/]")
        for name, cmd in custom_commands.items():
            first_line = cmd.template.strip().splitlines()[0] if cmd.template.strip() else ""
            preview = first_line[:80] + ("…" if len(first_line) > 80 else "")
            console.print(f"  [cyan]/{name}[/]  [dim]{preview}[/]")
        console.print(
            f"[dim]  (arquivos em {commands_dir()})[/]"
        )
    console.print(
        f"\n[dim]Tools disponíveis: {', '.join(registry.names())}[/]"
    )


# ---------------------------------------------------------------------------
# Typer app
# ---------------------------------------------------------------------------
@app.command()
def chat(
    profile: str = typer.Option(
        None,
        "--profile",
        "-p",
        help=f"Perfil de modelo. Disponíveis: {', '.join(PROFILES)}",
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        "-r",
        help="Retoma a última sessão deste cwd automaticamente.",
    ),
    no_resume_prompt: bool = typer.Option(
        False,
        "--no-resume-prompt",
        help="Não pergunta sobre retomar a sessão (default pergunta se houver).",
    ),
) -> None:
    """Abre o REPL interativo do mtzcode."""
    cfg = Config.load()
    if profile:
        try:
            cfg = cfg.with_profile(get_profile(profile))
        except KeyError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
    _repl(cfg, resume=resume, ask_resume=not no_resume_prompt)


@app.command()
def profiles() -> None:
    """Lista todos os perfis de modelo disponíveis."""
    for p in list_profiles():
        locality = "[green]local[/]" if p.is_local else "[red]cloud[/]"
        console.print(
            f"[bold cyan]{p.name}[/] — [bold]{p.label}[/] ({locality})\n"
            f"  [dim]{p.description}[/]\n"
            f"  [dim]modelo:[/] {p.model}\n"
            f"  [dim]host:[/]   {p.base_url}\n"
        )


@app.command()
def index(
    path: str = typer.Option(
        ".", "--path", "-p", help="Diretório raiz a indexar (default: cwd)."
    ),
    clear: bool = typer.Option(
        False, "--clear", help="Apaga o índice antes de reindexar."
    ),
) -> None:
    """Indexa o projeto pra busca semântica (RAG).

    Gera embeddings locais via nomic-embed-text e persiste em
    `<root>/.mtzcode/index.db`. Reindexa incremental: só toca arquivos
    modificados desde a última rodada.
    """
    from mtzcode.rag import EmbeddingClient, EmbeddingError, Index, ProjectIndexer

    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        console.print(f"[red]diretório inválido:[/] {root}")
        raise typer.Exit(1)

    db_path = root / ".mtzcode" / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    console.print(
        f"[dim]raiz:[/] {root}\n[dim]índice:[/] {db_path}\n"
    )

    try:
        embedder = EmbeddingClient()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]erro iniciando embedder:[/] {exc}")
        raise typer.Exit(1) from exc

    idx = Index(db_path)
    if clear:
        idx.clear()
        console.print("[dim]índice limpo.[/]")

    indexer = ProjectIndexer(root, idx, embedder)

    try:
        with console.status("[dim]indexando...[/]", spinner="dots") as status:
            def on_progress(p, rel):
                status.update(
                    f"[dim]indexando {p.files_indexed} arquivos / "
                    f"{p.chunks_created} chunks — último: {rel}[/]"
                )
            progress = indexer.index_project(on_progress=on_progress)
    except EmbeddingError as exc:
        console.print(f"[red]erro no embedder:[/] {exc}")
        raise typer.Exit(1) from exc
    finally:
        idx.close()
        embedder.close()

    stats = Index(db_path).stats()
    console.print(
        f"[green]✓ indexado[/]\n"
        f"  arquivos indexados: [bold]{progress.files_indexed}[/] "
        f"de {progress.files_scanned} escaneados\n"
        f"  chunks criados:     [bold]{progress.chunks_created}[/]\n"
        f"  arquivos pulados:   [dim]{progress.files_skipped}[/]\n"
        f"  tamanho do índice:  [dim]{stats.db_size_bytes / 1024:.0f} KB[/]"
    )


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host do servidor."),
    port: int = typer.Option(8765, "--port", "-P", help="Porta do servidor."),
) -> None:
    """Inicia a interface web do mtzcode no navegador."""
    try:
        from mtzcode.web.server import run as run_server
    except ImportError as exc:
        console.print(
            f"[red]dependências da UI web não instaladas:[/] {exc}\n"
            "rode: [cyan]uv pip install -e .[/]"
        )
        raise typer.Exit(1) from exc
    url = f"http://{host}:{port}"
    console.print(
        Panel.fit(
            f"[bold cyan]mtzCode Web UI[/]\n\n"
            f"abra no navegador: [bold]{url}[/]\n"
            f"[dim]Ctrl+C para parar[/]",
            border_style="cyan",
        )
    )
    run_server(host=host, port=port)


@knowledge_app.command("add")
def knowledge_add(
    folder: str = typer.Argument(..., help="Pasta com os documentos a ingerir."),
    name: str = typer.Option(..., "--name", "-n", help="Nome da knowledge base."),
    clear: bool = typer.Option(
        False, "--clear", help="Apaga a base antes de recriar."
    ),
) -> None:
    """Cria ou atualiza uma knowledge base com documentos de uma pasta.

    Suporta: .md, .txt, .rst, .yaml, .json, .csv, .html, .sql, .py, .js,
    .pdf (se pypdf instalado), .docx (se python-docx instalado).
    """
    from mtzcode.knowledge import ingest_folder, knowledge_db_path
    from mtzcode.rag import EmbeddingClient, EmbeddingError

    src = Path(folder).expanduser().resolve()
    if not src.exists() or not src.is_dir():
        console.print(f"[red]pasta inválida:[/] {src}")
        raise typer.Exit(1)

    db_path = knowledge_db_path(name)
    console.print(f"[dim]fonte: {src}[/]")
    console.print(f"[dim]destino: {db_path}[/]\n")

    try:
        embedder = EmbeddingClient()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]erro iniciando embedder:[/] {exc}")
        raise typer.Exit(1) from exc

    try:
        with console.status("[dim]ingerindo...[/]", spinner="dots") as status:
            def on_progress(p, rel):
                status.update(
                    f"[dim]{p.files_ingested} arquivos / "
                    f"{p.chunks_created} chunks — {rel}[/]"
                )
            stats = ingest_folder(
                name, src, embedder, on_progress=on_progress, clear_first=clear
            )
    except EmbeddingError as exc:
        console.print(f"[red]erro no embedder:[/] {exc}")
        raise typer.Exit(1) from exc
    finally:
        embedder.close()

    console.print(
        f"[green]✓ knowledge base '{name}' atualizada[/]\n"
        f"  arquivos ingeridos: [bold]{stats.files_ingested}[/] "
        f"de {stats.files_scanned} escaneados\n"
        f"  chunks criados:     [bold]{stats.chunks_created}[/]\n"
        f"  pulados:            [dim]{stats.files_skipped}[/]"
    )


@knowledge_app.command("list")
def knowledge_list() -> None:
    """Lista todas as knowledge bases existentes."""
    from mtzcode.knowledge import knowledge_dir, list_knowledge_bases

    bases = list_knowledge_bases()
    if not bases:
        console.print(
            f"[dim]nenhuma knowledge base em {knowledge_dir()}\n"
            "crie uma com: [cyan]mtzcode knowledge add --name <nome> <pasta>[/][/]"
        )
        return
    console.print("[bold]Knowledge bases:[/]")
    for name, path, size in bases:
        console.print(
            f"  [bold cyan]{name}[/]  [dim]({size / 1024:.0f} KB)[/]\n"
            f"    [dim]{path}[/]"
        )


@knowledge_app.command("remove")
def knowledge_remove(
    name: str = typer.Argument(..., help="Nome da knowledge base a remover."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Pula confirmação."),
) -> None:
    """Remove uma knowledge base permanentemente."""
    from mtzcode.knowledge import knowledge_db_path

    path = knowledge_db_path(name)
    if not path.exists():
        console.print(f"[red]knowledge base '{name}' não existe.[/]")
        raise typer.Exit(1)

    if not yes:
        confirm = typer.confirm(
            f"remover '{name}' permanentemente ({path})?", default=False
        )
        if not confirm:
            console.print("[dim]cancelado.[/]")
            raise typer.Exit(0)

    path.unlink()
    console.print(f"[green]✓[/] knowledge base '{name}' removida")


@knowledge_app.command("search")
def knowledge_search(
    query: str = typer.Argument(..., help="Consulta em linguagem natural."),
    name: str = typer.Option(..., "--name", "-n", help="Nome da knowledge base."),
    top_k: int = typer.Option(5, "--top-k", "-k", help="Quantos resultados."),
) -> None:
    """Busca numa knowledge base e imprime os hits (útil pra debug)."""
    from mtzcode.knowledge import search_knowledge_base

    try:
        hits = search_knowledge_base(name, query, top_k=top_k)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    if not hits:
        console.print(f"[dim](nenhum resultado em '{name}' para '{query}')[/]")
        return

    for i, h in enumerate(hits, start=1):
        console.print(
            f"\n[bold cyan][{i}][/] {h.path}:{h.start_line}-{h.end_line}  "
            f"[dim](score {h.score:.3f})[/]"
        )
        snippet = h.content.strip()
        if len(snippet) > 500:
            snippet = snippet[:500] + "..."
        console.print(f"  {snippet}")


@app.command()
def auto(
    task: str = typer.Argument(..., help="Tarefa pra executar autonomamente."),
    profile: str = typer.Option(None, "--profile", "-p", help="Perfil de modelo."),
    criteria: list[str] = typer.Option(
        None,
        "--criteria",
        "-c",
        help="Critérios de aceite. Ex: 'file_exists:saida.md' ou 'cmd_zero:pytest'.",
    ),
) -> None:
    """Executa uma tarefa em modo autônomo (sem perguntar nada).

    Confirmações são puladas automaticamente. Comandos perigosos (rm -rf /,
    sudo rm, force push em main, etc) ainda são bloqueados por uma blocklist.
    """
    from mtzcode.auto_cli import run_auto

    try:
        run_auto(task=task, criteria=list(criteria) if criteria else None, profile_name=profile)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]erro:[/] {exc}")
        raise typer.Exit(1) from exc


schedule_app = typer.Typer(help="Agenda tarefas automáticas (cron) pro mtzcode.")
app.add_typer(schedule_app, name="schedule")


@schedule_app.command("add")
def schedule_add(
    name: str = typer.Argument(..., help="Nome curto pra identificar."),
    cron: str = typer.Argument(..., help="Cron, ex: '0 9 * * 1-5' (9h, dias úteis)."),
    prompt: str = typer.Argument(..., help="Prompt que será executado."),
    profile: str = typer.Option(None, "--profile", "-p"),
) -> None:
    """Agenda uma tarefa recorrente."""
    from mtzcode.scheduler.cli_commands import add_task

    task_id = add_task(name=name, cron=cron, prompt=prompt, profile=profile)
    console.print(f"[green]✓[/] tarefa agendada: [bold]{task_id}[/]")


@schedule_app.command("list")
def schedule_list() -> None:
    """Lista todas as tarefas agendadas."""
    from mtzcode.scheduler.cli_commands import list_tasks

    tasks = list_tasks()
    if not tasks:
        console.print("[dim]nenhuma tarefa agendada.[/]")
        return
    for t in tasks:
        status = "[green]on[/]" if t.enabled else "[dim]off[/]"
        console.print(
            f"  [bold cyan]{t.id}[/] {status} [bold]{t.name}[/]\n"
            f"    cron: [yellow]{t.cron}[/]   última: [dim]{t.last_run or '—'}[/]\n"
            f"    [dim]{t.prompt[:80]}{'…' if len(t.prompt) > 80 else ''}[/]"
        )


@schedule_app.command("rm")
def schedule_rm(task_id: str = typer.Argument(...)) -> None:
    """Remove uma tarefa agendada."""
    from mtzcode.scheduler.cli_commands import remove_task

    remove_task(task_id)
    console.print(f"[green]✓[/] removida: {task_id}")


@schedule_app.command("run")
def schedule_run(task_id: str = typer.Argument(...)) -> None:
    """Roda uma tarefa agendada agora."""
    from mtzcode.scheduler.cli_commands import run_task_now

    ok, summary = run_task_now(task_id)
    style = "green" if ok else "red"
    console.print(f"[{style}]{'✓' if ok else '✖'}[/] {summary}")


daemon_app = typer.Typer(help="Daemon que executa as tarefas agendadas.")
app.add_typer(daemon_app, name="daemon")


@daemon_app.command("run")
def daemon_run() -> None:
    """Roda o daemon do scheduler em foreground."""
    from mtzcode.scheduler.daemon import SchedulerDaemon

    console.print("[dim]daemon iniciado. Ctrl+C pra parar.[/]")
    SchedulerDaemon().run_forever()


@daemon_app.command("install")
def daemon_install() -> None:
    """Instala o daemon como launchd agent (macOS)."""
    from mtzcode.scheduler.daemon import install_launchd

    install_launchd()
    console.print("[green]✓[/] daemon instalado no launchd.")


@daemon_app.command("uninstall")
def daemon_uninstall() -> None:
    """Remove o launchd agent."""
    from mtzcode.scheduler.daemon import uninstall_launchd

    uninstall_launchd()
    console.print("[green]✓[/] daemon removido.")


@daemon_app.command("status")
def daemon_status_cmd() -> None:
    """Mostra status do daemon."""
    from mtzcode.scheduler.daemon import daemon_status

    s = daemon_status()
    console.print(s)


@app.command()
def version() -> None:
    """Mostra a versão."""
    console.print(f"mtzcode {__version__}")


def main() -> None:
    if len(sys.argv) == 1:
        _repl(Config.load())
        return
    app()


if __name__ == "__main__":
    main()
