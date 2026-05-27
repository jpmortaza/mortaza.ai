"""Sistema de habilidades sob demanda do mtzcode.

Em vez de mandar o schema de TODAS as tools no contexto a cada turno
(pesado pra modelos locais — 24 tools = ~6-8k tokens de overhead),
o agent enxerga só duas meta-habilidades:

  - `listar_habilidades(categoria?)` — lista nomes + descrições curtas
    das habilidades disponíveis. O modelo chama isto pra descobrir
    o que pode fazer.
  - `usar_habilidade(nome, argumentos)` — invoca a habilidade real.

Vantagens:
  - Contexto base ~10x menor (~500 tokens em vez de ~6-8k).
  - Adicionar novas habilidades não infla o prompt do sistema.
  - Confirmações destrutivas continuam funcionando porque a
    `UseSkillTool` consulta `destructive` da tool interna em runtime.

A `SkillRegistry` é uma `ToolRegistry` especial: do ponto de vista do
agent, expõe só as 2 meta-habilidades; internamente mantém o registry
completo e despacha as chamadas reais.

Não confundir com `mtzcode.skills` (sistema de prompts/skills baseado
em diretórios — conceito diferente, herdado do design anterior).
"""
from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, Field

from mtzcode.tools.base import Tool, ToolError, ToolRegistry

# Tipo do callback de confirmação (mesma assinatura usada no Agent).
ConfirmCallback = Callable[[str, dict[str, Any]], bool]


# ----------------------------------------------------------------------
# Categorização — usada por listar_habilidades pra agrupar.
# ----------------------------------------------------------------------
SKILL_CATEGORIES: dict[str, set[str]] = {
    "filesystem": {
        "read", "write", "edit", "glob", "grep",
        "search_code", "search_knowledge",
    },
    "shell": {"bash"},
    "web": {"web_fetch", "web_search", "open_url", "browser"},
    "macos": {
        "applescript", "clipboard_read", "clipboard_write",
        "notify", "screenshot", "open_app",
    },
    "documentos": {
        "docx_read", "docx_write", "pdf_read", "pdf_write_md",
        "xlsx_read", "xlsx_write", "text_writer",
    },
}


def _categoria_de(nome: str) -> str:
    """Retorna a categoria de uma habilidade pelo nome."""
    for cat, nomes in SKILL_CATEGORIES.items():
        if nome in nomes:
            return cat
    if nome.startswith("mcp_"):
        return "mcp"
    return "outras"


# ----------------------------------------------------------------------
# Meta-habilidades expostas ao modelo
# ----------------------------------------------------------------------
class ListSkillsArgs(BaseModel):
    categoria: str | None = Field(
        None,
        description=(
            "Filtrar por categoria. Categorias: filesystem, shell, web, "
            "macos, documentos, mcp. Omita pra listar todas."
        ),
    )


class ListSkillsTool(Tool):
    name = "listar_habilidades"
    description = (
        "Lista as habilidades (ferramentas) disponíveis pra você usar. "
        "Chame ANTES de usar qualquer habilidade nova, pra descobrir o "
        "nome correto e a categoria. Pode filtrar por categoria. "
        "Categorias: filesystem (arquivos/código), shell (bash), web "
        "(navegador, busca), macos (sistema, clipboard, apps), "
        "documentos (docx/pdf/xlsx), mcp (conectores externos)."
    )
    Args = ListSkillsArgs
    destructive = False

    def __init__(self, inner: ToolRegistry) -> None:
        self.inner = inner

    def run(self, args: ListSkillsArgs) -> str:  # type: ignore[override]
        nomes = self.inner.names()
        if args.categoria:
            cat = args.categoria.lower()
            nomes = [n for n in nomes if _categoria_de(n) == cat]
            if not nomes:
                return f"nenhuma habilidade na categoria `{cat}`."

        # Agrupa por categoria pra ficar legível
        por_cat: dict[str, list[tuple[str, str, bool]]] = {}
        for nome in sorted(nomes):
            tool = self.inner.get(nome)
            desc = (tool.description or "").strip().split("\n")[0]
            if len(desc) > 140:
                desc = desc[:137] + "..."
            cat = _categoria_de(nome)
            por_cat.setdefault(cat, []).append((nome, desc, tool.destructive))

        partes: list[str] = [f"# {len(nomes)} habilidades disponíveis\n"]
        for cat in ("filesystem", "shell", "web", "macos", "documentos", "mcp", "outras"):
            if cat not in por_cat:
                continue
            partes.append(f"\n## {cat}")
            for nome, desc, destructive in por_cat[cat]:
                marca = " ⚠" if destructive else ""
                partes.append(f"- **{nome}**{marca} — {desc}")

        partes.append(
            "\n\nPra invocar uma habilidade, chame `usar_habilidade` com "
            "`nome` (string) e `argumentos` (objeto com os parâmetros)."
        )
        return "\n".join(partes)


class UseSkillArgs(BaseModel):
    nome: str = Field(..., description="Nome exato da habilidade a invocar.")
    argumentos: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Objeto com os argumentos da habilidade. Cada habilidade tem "
            "seu próprio schema — chame `listar_habilidades` se não souber."
        ),
    )


class UseSkillTool(Tool):
    name = "usar_habilidade"
    description = (
        "Invoca uma habilidade pelo nome com seus argumentos. Use depois "
        "de descobrir a habilidade certa via `listar_habilidades`. "
        "Exemplo: `usar_habilidade(nome='read', argumentos={'path': '/tmp/x.py'})`. "
        "Habilidades destrutivas (write, edit, bash, etc) podem pedir "
        "confirmação ao usuário antes de executar."
    )
    Args = UseSkillArgs
    destructive = False  # checagem real é feita por habilidade no run()

    def __init__(
        self,
        inner: ToolRegistry,
        confirm_cb: ConfirmCallback | None = None,
    ) -> None:
        self.inner = inner
        self.confirm_cb = confirm_cb

    def run(self, args: UseSkillArgs) -> str:  # type: ignore[override]
        try:
            tool = self.inner.get(args.nome)
        except ToolError as exc:
            return (
                f"ERRO: habilidade `{args.nome}` não existe. "
                f"Chame `listar_habilidades` pra ver as opções. ({exc})"
            )

        if tool.destructive and self.confirm_cb is not None:
            if not self.confirm_cb(args.nome, args.argumentos):
                return f"usuário recusou a habilidade `{args.nome}`."

        try:
            return tool.call(args.argumentos)
        except ToolError as exc:
            return f"ERRO em `{args.nome}`: {exc}"


# ----------------------------------------------------------------------
# SkillRegistry — registry que expõe só as 2 meta-habilidades pro modelo
# ----------------------------------------------------------------------
class SkillRegistry(ToolRegistry):
    """Wrapper de `ToolRegistry` que expõe ao modelo só `listar_habilidades`
    e `usar_habilidade`, mantendo todas as habilidades reais acessíveis
    via dispatch interno.

    Importante: pro agent, esta classe SE COMPORTA como um ToolRegistry
    com 2 tools. Mas internamente o `inner` tem todas elas — então
    chamadas via `usar_habilidade` funcionam normalmente.
    """

    def __init__(self, inner: ToolRegistry) -> None:
        self.inner = inner
        self.list_tool = ListSkillsTool(inner)
        self.use_tool = UseSkillTool(inner, confirm_cb=None)
        super().__init__([self.list_tool, self.use_tool])

    def set_confirm_cb(self, cb: ConfirmCallback | None) -> None:
        """Atualiza o callback de confirmação propagado pra UseSkillTool.

        Deve ser chamado depois de criar o Agent, passando o mesmo
        callback que o Agent usaria.
        """
        self.use_tool.confirm_cb = cb

    def add_skill(self, tool: Tool) -> None:
        """Adiciona uma habilidade ao inventário interno (não exposta como meta)."""
        self.inner.register(tool)

    def all_skill_names(self) -> list[str]:
        """Lista todas as habilidades disponíveis (não só as meta)."""
        return self.inner.names()
