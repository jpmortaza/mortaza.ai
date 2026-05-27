"""Tools de integração com GitHub via ``gh`` CLI.

Wrapper conveniente em cima do ``gh`` (GitHub CLI). Tudo que essas tools
fazem dá pra fazer com a tool ``bash``, mas como tools dedicadas ficam
mais descobríveis pelo modelo e mais previsíveis (validação de args,
mensagens de erro úteis, atalhos pro fluxo "subir esta pasta como repo
novo").

Tools expostas:
  - gh_clone           clona um repo
  - gh_repo_info       info sobre um repo (descrição, stars, langs)
  - gh_list_repos      lista repos do usuário (ou de outro user)
  - gh_push_folder     init+commit+create+push uma pasta local como repo novo
  - gh_analyze_repo    clona temporariamente e devolve um sumário (árvore + READMEs)

Auth: usa o login global do ``gh`` (``gh auth login``) — não precisa de
nenhuma env var no mtzcode. Se ``gh`` não estiver instalado ou não estiver
logado, as tools devolvem erro com instrução pra resolver.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError

DEFAULT_TIMEOUT = 120
MAX_OUTPUT = 12000


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _gh_path() -> str:
    """Acha o binário ``gh`` no PATH ou em locais comuns."""
    candidates = [
        shutil.which("gh"),
        "/opt/homebrew/bin/gh",
        "/usr/local/bin/gh",
        "/usr/bin/gh",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise ToolError(
        "GitHub CLI (gh) não encontrado. Instale com `brew install gh` "
        "e faça login com `gh auth login`."
    )


def _git_path() -> str:
    g = shutil.which("git") or "/usr/bin/git"
    if not Path(g).exists():
        raise ToolError("git não encontrado no PATH.")
    return g


def _run(cmd: list[str], cwd: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    """Roda um comando e devolve (rc, stdout, stderr)."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env={**os.environ},
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"comando excedeu timeout de {timeout}s: {' '.join(cmd)}") from None
    except OSError as exc:
        raise ToolError(f"falha ao executar {cmd[0]}: {exc}") from exc
    return r.returncode, r.stdout or "", r.stderr or ""


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncado em {limit} chars)"


def _ensure_authed() -> None:
    """Verifica se o gh está logado. Lança ToolError com instrução clara."""
    rc, _, err = _run([_gh_path(), "auth", "status"], timeout=10)
    if rc != 0:
        raise ToolError(
            "gh CLI não está autenticado. Rode no terminal: `gh auth login` "
            "e siga as instruções (escolha GitHub.com → HTTPS → login com browser)."
        )


# ======================================================================
# gh_clone
# ======================================================================
class GhCloneArgs(BaseModel):
    repo: str = Field(
        ...,
        description="Nome do repo no formato 'owner/repo' (ex: 'jpmortaza/mtzcode') ou URL completa.",
    )
    dest: Optional[str] = Field(
        None,
        description="Pasta de destino. Se vazio, clona no cwd com nome do repo.",
    )


class GhCloneTool(Tool):
    name = "gh_clone"
    destructive = False
    description = (
        "Clona um repositório do GitHub pra uma pasta local. Use `owner/repo` "
        "ou URL completa. Requer `gh` instalado e logado."
    )
    Args = GhCloneArgs

    def run(self, args: GhCloneArgs) -> str:  # type: ignore[override]
        gh = _gh_path()
        cmd = [gh, "repo", "clone", args.repo]
        if args.dest:
            cmd.append(args.dest)
        rc, out, err = _run(cmd, timeout=300)
        if rc != 0:
            raise ToolError(f"gh clone falhou (rc={rc}): {err.strip() or out.strip()}")
        target = args.dest or args.repo.split("/")[-1].replace(".git", "")
        return f"clonado em ./{target}\n{out.strip()}\n{err.strip()}".strip()


# ======================================================================
# gh_repo_info
# ======================================================================
class GhRepoInfoArgs(BaseModel):
    repo: str = Field(
        ...,
        description="'owner/repo' a inspecionar (ex: 'anthropics/claude-code').",
    )


class GhRepoInfoTool(Tool):
    name = "gh_repo_info"
    destructive = False
    description = (
        "Mostra info de um repo do GitHub: descrição, stars, linguagens, "
        "última atualização, default branch. Não clona — só consulta a API."
    )
    Args = GhRepoInfoArgs

    def run(self, args: GhRepoInfoArgs) -> str:  # type: ignore[override]
        gh = _gh_path()
        fields = "name,owner,description,stargazerCount,forkCount,languages,defaultBranchRef,updatedAt,url,isPrivate,licenseInfo"
        rc, out, err = _run([gh, "repo", "view", args.repo, "--json", fields])
        if rc != 0:
            raise ToolError(f"gh repo view falhou: {err.strip() or out.strip()}")
        return _truncate(out.strip())


# ======================================================================
# gh_list_repos
# ======================================================================
class GhListReposArgs(BaseModel):
    user: Optional[str] = Field(
        None,
        description="Usuário/org a listar. Se vazio, lista os SEUS repos.",
    )
    limit: int = Field(20, description="Quantos repos listar.", ge=1, le=200)


class GhListReposTool(Tool):
    name = "gh_list_repos"
    destructive = False
    description = (
        "Lista repos de um usuário/org no GitHub. Sem `user`, lista os seus."
    )
    Args = GhListReposArgs

    def run(self, args: GhListReposArgs) -> str:  # type: ignore[override]
        gh = _gh_path()
        cmd = [gh, "repo", "list", "--limit", str(args.limit)]
        if args.user:
            cmd.insert(2, args.user)
        rc, out, err = _run(cmd)
        if rc != 0:
            raise ToolError(f"gh repo list falhou: {err.strip() or out.strip()}")
        return _truncate(out.strip() or "(nenhum repo encontrado)")


# ======================================================================
# gh_push_folder — fluxo "sobe esta pasta como repo novo"
# ======================================================================
class GhPushFolderArgs(BaseModel):
    folder: str = Field(
        ...,
        description="Caminho absoluto ou relativo da pasta a publicar.",
    )
    repo_name: str = Field(
        ...,
        description="Nome do repo a criar (sem owner — vai pro seu user).",
    )
    description: Optional[str] = Field(
        None, description="Descrição curta do repo (opcional)."
    )
    private: bool = Field(
        True,
        description="Se True (padrão), cria como privado. False = público.",
    )
    commit_message: str = Field(
        "initial commit", description="Mensagem do primeiro commit."
    )


class GhPushFolderTool(Tool):
    name = "gh_push_folder"
    destructive = True
    description = (
        "Publica uma pasta local como um repo NOVO no GitHub. Faz tudo: "
        "git init (se preciso) → add → commit → gh repo create → push. "
        "Use quando o usuário quer 'subir essa pasta pro GitHub'."
    )
    Args = GhPushFolderArgs

    def run(self, args: GhPushFolderArgs) -> str:  # type: ignore[override]
        _ensure_authed()
        gh = _gh_path()
        git = _git_path()
        folder = Path(args.folder).expanduser().resolve()
        if not folder.exists() or not folder.is_dir():
            raise ToolError(f"pasta não existe: {folder}")

        steps: list[str] = []

        # 1) git init se ainda não for um repo
        if not (folder / ".git").exists():
            rc, out, err = _run([git, "init"], cwd=str(folder))
            if rc != 0:
                raise ToolError(f"git init falhou: {err.strip()}")
            steps.append("git init ✓")
        else:
            steps.append("(já era um repo git)")

        # 2) add tudo
        rc, _, err = _run([git, "add", "-A"], cwd=str(folder))
        if rc != 0:
            raise ToolError(f"git add falhou: {err.strip()}")
        steps.append("git add ✓")

        # 3) commit (ignora erro se já não houver mudanças)
        rc, out, err = _run(
            [git, "commit", "-m", args.commit_message],
            cwd=str(folder),
        )
        if rc != 0 and "nothing to commit" not in (out + err).lower():
            raise ToolError(f"git commit falhou: {err.strip() or out.strip()}")
        steps.append("git commit ✓")

        # 4) gh repo create + push
        cmd = [
            gh, "repo", "create", args.repo_name,
            "--source", str(folder),
            "--push",
            "--private" if args.private else "--public",
        ]
        if args.description:
            cmd.extend(["--description", args.description])
        rc, out, err = _run(cmd, cwd=str(folder), timeout=300)
        if rc != 0:
            raise ToolError(
                f"gh repo create falhou: {err.strip() or out.strip()}\n"
                "Se o repo já existir, use outro nome ou apague o existente."
            )
        steps.append("gh repo create + push ✓")
        url = (out + err).strip().splitlines()[-1] if (out + err).strip() else ""
        return "\n".join(steps) + (f"\n\n→ {url}" if url else "")


# ======================================================================
# gh_analyze_repo — clona temporário, devolve sumário
# ======================================================================
class GhAnalyzeRepoArgs(BaseModel):
    repo: str = Field(..., description="'owner/repo' a analisar.")
    max_files: int = Field(
        80, description="Limite de arquivos listados na árvore.", ge=10, le=500
    )


class GhAnalyzeRepoTool(Tool):
    name = "gh_analyze_repo"
    destructive = False
    description = (
        "Clona um repo em pasta temporária, devolve árvore de arquivos + "
        "conteúdo do README e package.json/pyproject/Cargo.toml/go.mod (se "
        "existirem). Útil pra ter um overview rápido de um projeto sem "
        "deixar a pasta no disco. Limpa a pasta no final."
    )
    Args = GhAnalyzeRepoArgs

    def run(self, args: GhAnalyzeRepoArgs) -> str:  # type: ignore[override]
        gh = _gh_path()
        with tempfile.TemporaryDirectory(prefix="mtz-gh-") as tmp:
            target = Path(tmp) / "repo"
            rc, out, err = _run(
                [gh, "repo", "clone", args.repo, str(target), "--", "--depth", "1"],
                timeout=300,
            )
            if rc != 0:
                raise ToolError(f"gh clone falhou: {err.strip() or out.strip()}")

            parts: list[str] = [f"# {args.repo}\n"]

            # Árvore (ignora .git, node_modules, venvs)
            ignore = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next"}
            files: list[str] = []
            for p in sorted(target.rglob("*")):
                if any(part in ignore for part in p.relative_to(target).parts):
                    continue
                if p.is_file():
                    files.append(str(p.relative_to(target)))
                if len(files) >= args.max_files:
                    break
            parts.append(f"## Arquivos ({len(files)})\n```\n" + "\n".join(files) + "\n```\n")

            # Manifests/READMEs
            interesting = [
                "README.md", "README.rst", "README.txt", "README",
                "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
                "Gemfile", "composer.json", "build.gradle", "pom.xml",
                "Dockerfile", "docker-compose.yml",
            ]
            for name in interesting:
                fp = target / name
                if fp.exists() and fp.is_file():
                    try:
                        content = fp.read_text(encoding="utf-8", errors="replace")[:3000]
                    except OSError:
                        continue
                    parts.append(f"## {name}\n```\n{content}\n```\n")

            return _truncate("\n".join(parts))
