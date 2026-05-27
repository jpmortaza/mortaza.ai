# mortaza.ai

> Agente de código + chat WhatsApp. Junta o **mtzcode** (loop de agente local
> com tool calling, RAG, skills) com **Claude API** e a **Evolution API**
> (WhatsApp). Conversa via WhatsApp, terminal ou web; executa código no
> servidor. Local-first por padrão, cloud opcional.

## 🚀 Quick start — Bot WhatsApp + Claude

Pré-requisitos: Docker, Evolution API rodando, e uma `ANTHROPIC_API_KEY`.

```bash
cp .env.example .env
# edite .env com ANTHROPIC_API_KEY + EVOLUTION_* + WHATSAPP_ALLOWED_NUMBERS
docker compose up -d --build
docker compose logs -f mortaza-bot
```

Configurar o webhook da Evolution apontando pra:

```
http://<host-do-bot>:8000/whatsapp/webhook
```

(eventos: `messages.upsert` — basta esse pra o MVP)

Manda uma mensagem no WhatsApp pro número da instância. O bot responde.
Comandos suportados na conversa: `/reset`, `/help`.

Healthcheck: `GET http://localhost:8000/whatsapp/health`

Veja a seção [Integração WhatsApp](#integração-whatsapp-evolution-api) abaixo
para detalhes (segurança, profile, multi-contato, etc).

---

## mtzcode (base)

> Assistente de código **100% local por padrão**, com agent loop, tool calling,
> RAG e interface web estilo app do Claude. Sem APIs externas obrigatórias,
> sem custos por uso, sem código saindo da sua máquina.

```
███╗   ███╗████████╗███████╗ ██████╗ ██████╗ ██████╗ ███████╗
████╗ ████║╚══██╔══╝╚══███╔╝██╔════╝██╔═══██╗██╔══██╗██╔════╝
██╔████╔██║   ██║     ███╔╝ ██║     ██║   ██║██║  ██║█████╗
██║╚██╔╝██║   ██║    ███╔╝  ██║     ██║   ██║██║  ██║██╔══╝
██║ ╚═╝ ██║   ██║   ███████╗╚██████╗╚██████╔╝██████╔╝███████╗
╚═╝     ╚═╝   ╚═╝   ╚══════╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝
                              by Jean Mortaza
```

## Índice

- [O que é](#o-que-é)
- [Capacidades](#capacidades)
- [Quick start (TL;DR)](#quick-start-tldr)
- [Walkthrough do zero](#walkthrough-do-zero)
- [Interface web](#-interface-web-recomendado)
- [Interface CLI](#-interface-cli-terminal)
- [Skills — especialistas plugáveis](#-skills--especialistas-plugáveis)
- [Knowledge base — memória permanente](#-knowledge-base--memória-permanente)
- [RAG — busca semântica no projeto](#-rag--busca-semântica-no-projeto)
- [Perfis de modelo](#perfis-de-modelo)
- [Slash commands customizados](#slash-commands-customizados)
- [Modo plano (plan mode)](#modo-plano-plan-mode)
- [Troubleshooting](#troubleshooting)
- [Configuração (env vars)](#configuração-env-vars)
- [Arquitetura](#arquitetura)
- [Roadmap](#roadmap)

---

## O que é

Um clone enxuto do estilo **Claude Code**, mas que conversa com um modelo
rodando **localmente** via [Ollama](https://ollama.com) — usando a capacidade
de processamento do seu próprio notebook. Por padrão, **nada sai da máquina**.

Tem **duas interfaces**:

- **Web UI** (recomendado) — `mtzcode serve` abre um servidor local e você
  conversa pelo navegador, com streaming de tokens em tempo real, tool calls
  clicáveis, syntax highlight e troca de modelo quente.
- **CLI (terminal)** — `mtzcode` abre um REPL no próprio terminal, com UI
  bonita via Rich, diffs coloridos, confirmação destrutiva, e comandos tipo
  `/plano`, `/indexar`, `/modelo`.

O mesmo **agent loop** está por baixo das duas: envia a mensagem pro modelo,
parseia tool calls, executa, devolve o resultado pro modelo, repete até ter
uma resposta final — igual ao Claude Code.

## Capacidades

- ✅ **Duas interfaces** — web UI no navegador ou REPL no terminal
- ✅ **Agent loop** com tool calling iterativo (estilo Claude Code)
- ✅ **Stack-agnóstico** — não é só Python. Cria projeto em Next.js, Go, Rust, Flutter, Ruby, PHP, Java, Swift… o modelo escolhe a stack certa pro pedido.
- ✅ **Tools nativas** (grupo `core`):
  - `read` / `write` / `edit` — arquivos com diff unified colorido
  - `bash` — executa comandos shell com timeout e confirmação destrutiva
  - `python_exec` — sandbox isolado em subprocess pra rodar Python sem precisar do bash
  - `glob` / `grep` / `search_code` — busca por nome, conteúdo e semântica (RAG)
  - `search_knowledge` — busca semântica em knowledge bases permanentes
  - `web_fetch` / `open_url` — abrir páginas e baixar conteúdo
  - `todo_write` / `todo_read` — TODO list persistente espelhada na UI
  - `plan_task` / `plan_show` / `plan_set_status` / `plan_advance` / `plan_list` — **orquestrador** (Planner) pra projetos do PRD ao deploy
  - `spawn_agent` — **sub-agentes** (Executor) com contexto e tools isolados
- ✅ **Tool groups extras** (opt-in via `MTZCODE_TOOL_GROUPS`):
  - `github` — clone/info/push/analyze de repos via `gh` CLI
  - `web` — `web_search` + `browser` real
  - `apify` — scraping/automação via plataforma Apify
  - `macos` — clipboard, AppleScript, screenshot, abrir apps, notificações
  - `documents` — ler/escrever docx, pdf, xlsx
  - `superpowers` — Spotlight (find_files / find_images)
- ✅ **Orquestrador (Planner + Executor)** — pedidos grandes ("crie um SaaS de agendamento com login e Stripe") viram um plano estruturado em fases → tarefas, persistido em `~/.mtzcode/orchestrator/` e espelhado na aba **Tarefas** da UI. Sub-agentes podem ser spawnados pra executar partes isoladas (pesquisa web extensa, geração de arquivos parecidos) sem poluir o histórico do principal. Profundidade máxima de delegação = 3.
- ✅ **TODO list persistente** — `todo_write` mantém uma lista que sobrevive entre sessões e aparece em tempo real no painel direito da UI.
- ✅ **Sessões persistentes** — cada conversa vira `.jsonl` em `~/.mtzcode/logs/`. Auto-resume no CLI (detecta sessão mais recente do `cwd`) e listagem clicável na Web UI.
- ✅ **Fila de execução no chat** — pode mandar várias mensagens enquanto o assistente responde, vão sendo processadas em ordem.
- ✅ **Anexos no chat** — drag & drop ou botão de anexar arquivos direto no composer. O modelo recebe o caminho e usa `read` pra ler.
- ✅ **Atalho de voz** (`Cmd/Ctrl+Shift+M`) — ditado por voz com transcrição.
- ✅ **Skills** — pasta `skills/` com especialistas plugáveis (Python, SQL, code review, Git, campanhas eleitorais, social media…).
- ✅ **Knowledge base** — ingestão de PDFs, Markdowns, docs em geral como memória permanente.
- ✅ **Streaming real de tokens** (CLI e web).
- ✅ **Múltiplos perfis de modelo** com troca quente (CLI: `/modelo`, web: dropdown). Erro amigável quando o modelo não está puxado no Ollama ou não suporta tool calling.
- ✅ **RAG integrado** — indexação incremental com `nomic-embed-text`, busca semântica como tool.
- ✅ **Modo plano** (`/plano`) pra pesquisar sem executar.
- ✅ **Slash commands customizados** — `~/.mtzcode/commands/*.md`.
- ✅ **Fallback robusto** de parsing de tool calls (compatível com modelos Q4).
- ✅ **Mensagens em português brasileiro**.
- ✅ **Groq, Maritaca (Sabiá-3) opcionais** como fallback cloud quando precisar de velocidade.

## Quick start (TL;DR)

Se você já tem **Homebrew**, **Ollama**, **Python 3.12** e **uv** instalados:

```bash
brew install ollama ripgrep
brew services start ollama
ollama pull qwen2.5-coder:14b
ollama pull nomic-embed-text

git clone https://github.com/jpmortaza/mtzcode.git ~/mtzcode
cd ~/mtzcode
uv venv && uv pip install -e .

./bin/mtzcode serve        # abre web UI em http://localhost:8765
# OU
./bin/mtzcode              # abre REPL no terminal
```

---

## Walkthrough do zero

Do início, pra quem nunca instalou nada. Tempo estimado: **~30 minutos**
(a maior parte é esperar downloads de modelos).

### 1. Instale o Homebrew (se não tiver)

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### 2. Instale as dependências do sistema

```bash
brew install ollama ripgrep python@3.12 uv
```

- **ollama** — roda modelos de IA localmente
- **ripgrep** — busca rápida em arquivos (usado pela tool `grep`)
- **python@3.12** — linguagem do mtzcode
- **uv** — gerenciador de pacotes Python (10-100x mais rápido que pip)

### 3. Inicie o Ollama como serviço de background

```bash
brew services start ollama
```

Isso faz o Ollama subir automaticamente toda vez que o Mac liga.
Pra verificar:

```bash
curl http://localhost:11434/api/tags
# Deve retornar: {"models":[]}
```

### 4. Baixe os modelos

Os modelos principais:

```bash
ollama pull qwen2.5-coder:14b      # ~9 GB — modelo principal (recomendado)
ollama pull qwen2.5-coder:7b       # ~5 GB — alternativa leve (opcional)
ollama pull nomic-embed-text       # ~270 MB — embeddings pro RAG
```

> 💡 Se você tem só 8 GB de RAM, use só o `qwen2.5-coder:7b` como principal.

Pra confirmar que baixou:

```bash
ollama list
```

### 5. Clone o mtzcode

```bash
git clone https://github.com/jpmortaza/mtzcode.git ~/mtzcode
cd ~/mtzcode
```

### 6. Crie o venv e instale

```bash
uv venv
uv pip install -e .
```

Isso cria `.venv/` local e instala todas as dependências (httpx, rich, typer,
pydantic, numpy, fastapi, uvicorn).

### 7. Primeiro teste — CLI

```bash
./bin/mtzcode version
# Deve imprimir: mtzcode 0.0.1
```

Se imprimiu a versão, ótimo. Pra abrir o REPL:

```bash
./bin/mtzcode
```

Vai aparecer o banner gigante de ASCII + o painel com o profile ativo. Experimente:

```
você › olá, quem é você?
```

> ⏱️ **Primeira mensagem demora ~30s** — o Ollama tá carregando o modelo de 9 GB
> na memória (cold start). Próximas mensagens são muito mais rápidas.

### 8. Primeiro teste — Web UI

Em outro terminal:

```bash
cd ~/mtzcode
./bin/mtzcode serve
```

Vai aparecer:

```
╭──────────────────────╮
│ mtzCode Web UI       │
│                      │
│ abra no navegador:   │
│ http://127.0.0.1:8765│
│                      │
│ Ctrl+C para parar    │
╰──────────────────────╯
```

Abra **http://localhost:8765** no navegador.

### 9. Indexe o projeto (pra usar `search_code`)

Antes da busca semântica funcionar, você precisa gerar os embeddings:

```bash
./bin/mtzcode index
```

Saída esperada:

```
raiz: /Users/jeanmortaza/mtzcode
índice: /Users/jeanmortaza/mtzcode/.mtzcode/index.db

✓ indexado
  arquivos indexados: 27 de 28 escaneados
  chunks criados:     123
  arquivos pulados:   0
  tamanho do índice:  584 KB
```

Pronto. Agora o agent pode chamar `search_code` pra achar código por significado,
não só por palavra-chave.

### 10. (Opcional) Adicione `mtzcode` ao PATH

Pra chamar de qualquer diretório:

```bash
echo 'export PATH="$HOME/mtzcode/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
mtzcode         # funciona em qualquer lugar agora
```

---

## 🌐 Interface web (recomendado)

```bash
mtzcode serve
```

Abre em **http://localhost:8765**. Layout:

```
┌──────────────────────────────────────────────────────────────────┐
│ mtzCode  [perfil ▼] [local]  [pasta: /Users/...]  [ / limpar ]   │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│   [JM] você                                                      │
│        liste os arquivos .py e me diga qual é o maior            │
│                                                                  │
│   [mtz] mtzcode                                                  │
│         → tool glob (pattern='*.py')            ▶                │
│         → tool bash (command='wc -l ...')       ▶                │
│         O maior arquivo é `mtzcode/cli.py` com 421 linhas.       │
│                                                                  │
├──────────────────────────────────────────────────────────────────┤
│ [pergunte aqui...                             ]  [Enviar ↵]     │
│ 100% local · pronto                                              │
└──────────────────────────────────────────────────────────────────┘
```

### Recursos da Web UI

- **Seletor de pasta de trabalho** — cole o **caminho absoluto** do projeto
  onde o agent vai trabalhar. Ao aplicar (✓ ou Enter), o servidor faz `chdir`
  pra essa pasta, e todas as tools passam a operar nela.
- **Dropdown de perfil** — troca quente entre qwen-14b, qwen-7b, groq-llama.
  Indicador **local**/**cloud** ao lado.
- **Chat com streaming real** — cada token chega no instante em que sai do
  modelo (não espera a resposta completa).
- **Tool calls colapsáveis** — cada chamada aparece como um card clicável;
  expande pra ver os argumentos e o resultado completo.
- **Markdown + syntax highlight** — respostas com código formatado e
  destacado via highlight.js.
- **Empty state com hints** — 4 cards com exemplos prontos pra começar.
- **Botão `/ limpar`** — zera o histórico do agent.

### Flags opcionais

```bash
mtzcode serve --host 0.0.0.0 --port 9000    # porta customizada, exposto na LAN
mtzcode serve -h 127.0.0.1 -P 8765          # default (só localhost)
```

> ⚠️ **Não exponha na rede pública** (`--host 0.0.0.0`) sem adicionar auth —
> a UI não tem nenhum controle de acesso.

---

## 💻 Interface CLI (terminal)

```bash
mtzcode                    # abre o REPL com profile default (qwen-14b)
mtzcode chat -p qwen-7b    # REPL com profile específico
mtzcode profiles           # lista todos os perfis
mtzcode index              # indexa o projeto (pra search_code)
mtzcode index --clear      # zera e reindexa do zero
mtzcode serve              # sobe a web UI
mtzcode version            # mostra a versão
```

### Comandos do REPL

| Comando | Ação |
|---|---|
| `/sair` | Encerra a sessão (ou `Ctrl+C`/`Ctrl+D`) |
| `/limpar` | Zera o histórico da conversa e flags de plan mode |
| `/modelo` | Abre menu interativo pra trocar de perfil (mantém histórico) |
| `/plano` | Ativa [modo plano](#modo-plano-plan-mode) — bloqueia tools destrutivas |
| `/executar` | Sai do modo plano, libera tools destrutivas |
| `/indexar` | Indexa o cwd atual pra busca semântica (RAG) |
| `/ajuda` | Mostra ajuda, tools disponíveis e slash commands customizados |

### Exemplos de pedidos

```
você › leia o arquivo mtzcode/agent.py e me explique o que faz

você › encontre todos os TODOs do projeto

você › crie um arquivo hello.py com uma função fizzbuzz

você › rode "git status" e resuma o que está pendente

você › qual o maior arquivo .py deste projeto?

você › /plano
você › como eu adicionaria suporte a websockets no servidor?
  (o agent pesquisa e entrega plano sem tocar em nada)

você › onde está a lógica de parsing de tool calls?
  (com RAG indexado, o agent usa search_code pra achar por significado)
```

O agent decide sozinho que tools usar, executa, processa, responde. Você
só pede em linguagem natural.

---

## 🎓 Skills — especialistas plugáveis

**Skills** são prompts especializados que redefinem o comportamento do agent
pra uma tarefa ou domínio específico. Quando você ativa uma skill, o mtzcode
"vira" um especialista naquela área.

### Usar uma skill existente

```
você › /skill
Skills disponíveis:
    python-expert (oficial) — Especialista em Python moderno...
    javascript-expert (oficial) — Especialista em JS/TS...
    sql-dba (oficial) — DBA, otimiza queries...
    code-reviewer (oficial) — Code review crítico...
    git-helper (oficial) — Git, conflitos, histórico...
    writer-pt (oficial) — Revisa textos em português...

você › /skill python-expert
✓ skill ativada: python-expert

você › como eu faço retry com backoff exponencial?
  (agora o agent responde como um especialista em Python moderno)

você › /skill off
✓ skill desativada
```

### Skills oficiais (vêm com o mtzcode)

| Skill | Descrição |
|---|---|
| `python-expert` | Python moderno, PEP 8, type hints, async, testing |
| `javascript-expert` | JS/TS moderno, Node 20+, React, tooling |
| `sql-dba` | Queries, otimização, EXPLAIN plans, indexação |
| `code-reviewer` | Review crítico focado em bugs e segurança |
| `git-helper` | Comandos git, conflitos, recuperação de commits |
| `writer-pt` | Revisão de textos em português brasileiro |

Estão em [`skills/`](skills/) no repositório — cada uma é um diretório com
um `SKILL.md`.

### Criar sua própria skill

**Pessoal** (não compartilhada):

```bash
mkdir -p ~/.mtzcode/skills/minha-skill
cat > ~/.mtzcode/skills/minha-skill/SKILL.md <<'EOF'
---
name: minha-skill
description: Descrição curta da skill
author: seu-nome
---

# Minha Skill

Você é um especialista em [X]. Siga estas regras:

1. ...
2. ...

## Estilo de resposta
...
EOF
```

Recarregue o REPL e ela aparece em `/skill`.

**Contribuir pro repo oficial:**

1. Fork este repositório
2. Crie `skills/sua-skill/SKILL.md`
3. Abra um PR

Detalhes completos do formato em [skills/README.md](skills/README.md).

---

## 📚 Knowledge base — memória permanente

Diferente do RAG de código, uma **knowledge base** é uma memória permanente
de **documentos arbitrários**: política da empresa, manuais internos, PDFs
de procedimentos, notas pessoais, documentação de um fornecedor, etc.

Você pode ter **várias** (`empresa`, `projeto-x`, `receitas`) e o agent busca
nelas via a tool `search_knowledge`.

### Formatos suportados

- **Texto:** `.md`, `.txt`, `.rst`, `.yaml`, `.json`, `.csv`, `.html`, `.sql`, `.py`, `.js` (e muitos outros)
- **PDF:** `.pdf` (requer `pypdf` — instalado com `uv pip install -e .[docs]`)
- **Word:** `.docx` (requer `python-docx` — idem)

### Criar uma knowledge base

```bash
# Instale os extras se for usar PDF/docx
uv pip install -e .[docs]

# Ingestão — aponta pra uma pasta com os documentos
mtzcode knowledge add --name empresa ~/docs/minha-empresa
```

Saída:

```
fonte: /Users/jeanmortaza/docs/minha-empresa
destino: /Users/jeanmortaza/.mtzcode/knowledge/empresa.db

✓ knowledge base 'empresa' atualizada
  arquivos ingeridos: 47 de 50 escaneados
  chunks criados:     312
  pulados:            3
```

### Comandos `knowledge`

```bash
mtzcode knowledge add --name empresa ~/docs          # ingerir uma pasta
mtzcode knowledge add --name empresa ~/docs --clear  # recomeçar do zero
mtzcode knowledge list                                # listar bases
mtzcode knowledge search "quantos dias de férias" --name empresa
mtzcode knowledge remove empresa                      # apagar permanente
```

### Usar no REPL / Web UI

Depois de criar a base, é só perguntar naturalmente. O agent decide sozinho
quando usar `search_knowledge`:

```
você › qual a política de home office da empresa?
  → tool search_knowledge (base='empresa', query='política de home office')
  ← [1] politicas.md:1-13  (score 0.71)
      Home Office: funcionários podem trabalhar remoto até 3 dias...

  A política de home office permite até 3 dias remotos por semana.
  Sextas são remotas por padrão.
```

### Onde os dados ficam

Cada base vira um arquivo SQLite em `~/.mtzcode/knowledge/<nome>.db`.
Backup e portabilidade triviais — é só um arquivo.

### Casos de uso

- **Empresa:** políticas, processos, código de conduta, manuais de
  sistemas internos, documentação de fornecedores
- **Projeto:** specs, decisões arquiteturais, RFCs antigas, contexto histórico
- **Pessoal:** notas, bookmarks, receitas, anotações de estudo
- **Legal/compliance:** contratos, políticas regulatórias

> ⚠️ **Tudo fica local.** O mtzcode nunca envia seus documentos pra nenhum
> servidor externo. O embedding é gerado pelo `nomic-embed-text` rodando no
> Ollama local, e o índice é SQLite.

---

## 🔍 RAG — busca semântica no projeto

**Problema:** `grep` só acha palavras literais. Se você pergunta "onde é feita
a autenticação?" num projeto que usa a palavra "auth" (nunca "autenticação"),
grep falha.

**Solução:** **RAG** (Retrieval-Augmented Generation). O mtzcode gera
**embeddings** de todos os arquivos do projeto com `nomic-embed-text` (roda
local no Ollama), guarda em SQLite, e expõe uma tool `search_code` que busca
por **similaridade semântica**.

### Como usar

**1. Indexe o projeto (uma vez por projeto):**

```bash
cd ~/meu-projeto
mtzcode index
```

Ou dentro do REPL:

```
você › /indexar
```

Isso gera `.mtzcode/index.db` na raiz do projeto. A indexação é **incremental**:
só reprocessa arquivos cujo `mtime` mudou desde a última rodada.

**2. Deixe o agent usar a tool naturalmente:**

```
você › onde o parser de tool calls está implementado?
  → tool search_code (query='parser de tool calls', top_k=5)
  ← [1] mtzcode/agent.py:200-280  (score 0.734)
      def _extract_tool_calls_from_content(content: str) ...
  ...
  O parser está em mtzcode/agent.py nas funções
  `_extract_tool_calls_from_content` (linha 220) e
  `_extract_top_level_json_objects` (linha 290).
```

### Quando o RAG é útil

- ✅ Perguntas conceituais: "onde é feita X?", "qual arquivo controla Y?"
- ✅ Projetos grandes onde você não sabe o nome exato das funções
- ✅ Refactor: "quais lugares usam esse padrão?"
- ❌ Buscas por string literal — use `grep` direto, é mais rápido
- ❌ Projetos pequenos (< 20 arquivos) — o agent já consegue ler tudo

### Configuração do RAG

| Var | Padrão | Descrição |
|---|---|---|
| `MTZCODE_EMBED_MODEL` | `nomic-embed-text` | Modelo de embeddings no Ollama |
| `MTZCODE_EMBED_HOST` | `http://localhost:11434` | Host do Ollama |

### Reindexar após mudanças grandes

A indexação incremental lida com edições normais automaticamente. Mas se
você fez um refactor enorme ou quer começar do zero:

```bash
mtzcode index --clear
```

---

## Perfis de modelo

| Perfil | Backend | Modelo | Tipo | Uso recomendado |
|---|---|---|---|---|
| `qwen-14b` | Ollama | qwen2.5-coder:14b | 🟢 local | **Default.** Melhor qualidade. ~25 tok/s no M4. |
| `qwen-7b` | Ollama | qwen2.5-coder:7b | 🟢 local | Mais rápido (~50 tok/s), bom pra tarefas simples. |
| `groq-llama` | Groq cloud | llama-3.3-70b-versatile | 🔴 cloud | Super rápido (~300 tok/s). **Atenção: dados saem da máquina.** |

### Trocar perfil na Web UI

No header, clique no dropdown "perfil" e escolha. O badge ao lado muda pra
indicar `local` ou `cloud`.

### Trocar perfil no REPL

```
você › /modelo

Perfis disponíveis:
  ● 1. Qwen 2.5 Coder 14B (local)
    2. Qwen 2.5 Coder 7B (local, leve)
    3. Llama 3.3 70B (Groq cloud)

escolha um perfil › 2
✓ agora usando Qwen 2.5 Coder 7B (histórico preservado)
```

### Usar Groq (cloud, opcional)

O profile `groq-llama` envia as mensagens pra nuvem do Groq. Útil quando
você precisa de velocidade (~300 tok/s) mas **quebra a garantia de privacidade local**.

1. Crie uma conta em [console.groq.com](https://console.groq.com)
2. Gere uma API key
3. Adicione ao `~/.zshrc`:
   ```bash
   export GROQ_API_KEY=gsk_...
   ```
4. Reinicie o terminal (`source ~/.zshrc` ou feche e abra)
5. Use: `mtzcode chat -p groq-llama`

---

## Slash commands customizados

Crie arquivos `.md` em `~/.mtzcode/commands/`. Cada arquivo vira um comando
no REPL. Use `$ARGUMENTS` pra injetar o que o usuário digitar depois do comando.

**Exemplo — `~/.mtzcode/commands/review.md`:**

```markdown
Faça uma revisão crítica do código em $ARGUMENTS. Aponte bugs potenciais,
problemas de segurança, e sugestões de melhoria. Seja direto e conciso.
Liste no formato:

1. [CRÍTICO|IMPORTANTE|MENOR] — descrição — linha
```

**Uso no REPL:**

```
você › /review mtzcode/agent.py
→ expandindo /review (237 chars)
```

E o agent recebe o template expandido como se você tivesse digitado a mensagem
inteira.

**Outros exemplos úteis:**

- `~/.mtzcode/commands/testes.md` → template pra gerar testes
- `~/.mtzcode/commands/docstrings.md` → template pra adicionar docstrings
- `~/.mtzcode/commands/explicar.md` → template pra explicação pedagógica

Pra usar um diretório customizado:

```bash
export MTZCODE_COMMANDS_DIR=~/projetos/meus-commands
```

---

## Modo plano (plan mode)

Quando você quer que o agent **pesquise e planeje antes de executar** — útil
pra tarefas complexas onde você prefere aprovar a abordagem antes de tocar
em arquivos.

```
você › /plano
◐ modo plano ativado — tools destrutivas bloqueadas. Digite /executar pra sair.

você › como eu adicionaria um sistema de cache de respostas?

  → tool glob (pattern='**/*.py')
  → tool read (path='mtzcode/client.py')
  → tool grep (pattern='chat')

  ## Plano: adicionar cache de respostas

  **Contexto:** ...
  **Passos:**
    1. Criar mtzcode/cache.py com CacheClient (hash do payload)
    2. Modificar ChatClient.chat() pra consultar cache antes
    3. ...

você › aprovado, pode fazer
você › /executar
▶ modo plano desativado — tools destrutivas liberadas novamente.

você › implemente o plano
  (agora ele pode usar write/edit/bash)
```

Em plan mode, `write`, `edit` e `bash` **ficam bloqueadas** — se o modelo
tentar usar uma delas, o confirm_cb recusa automaticamente.

---

## Troubleshooting

### 🐛 "Servidor respondendo com lixo JSON misturado, ou muito lento"

**Sintoma:** você vê algo tipo:
```
{"name": "read", "arguments": {"path": "./"}}{"message": "Olá..."}
```

**Causa:** você está com um servidor antigo rodando (antes dos fixes mais
recentes). O Python cacheia módulos quando importados — o processo em memória
ainda tem o código velho mesmo que os arquivos já estejam atualizados.

**Fix:**

```bash
# 1. Encontre o processo do servidor
lsof -i :8765

# 2. Mate ele (substitua PID pelo número mostrado)
kill <PID>

# OU mate qualquer processo rodando na porta 8765 direto:
lsof -ti:8765 | xargs -r kill

# 3. Suba um novo
cd ~/mtzcode
git pull                      # pega as últimas mudanças
uv pip install -e .           # reinstala se houve mudança de deps
./bin/mtzcode serve
```

**Depois recarregue a aba do navegador** (Cmd+R).

### 🐛 "First request takes 30+ seconds"

Isso é **esperado** — o Ollama precisa carregar o modelo de 9 GB na memória
(cold start). Requisições subsequentes usam o modelo já carregado e são muito
mais rápidas.

Pra manter o modelo sempre quente, você pode enviar um ping periódico:

```bash
while true; do
  curl -s http://localhost:11434/api/chat \
    -d '{"model":"qwen2.5-coder:14b","messages":[{"role":"user","content":"oi"}],"stream":false}' \
    > /dev/null
  sleep 240  # Ollama descarrega após 5min ocioso, então ping a cada 4min
done &
```

### 🐛 "erro ao conectar no Ollama em http://localhost:11434"

O serviço do Ollama não está rodando. Inicie:

```bash
brew services start ollama
# ou manualmente:
ollama serve
```

### 🐛 "variável GROQ_API_KEY não definida"

Você tentou usar o profile `groq-llama` sem configurar a chave. Opções:

1. **Voltar pro local:** `mtzcode chat -p qwen-14b`
2. **Configurar Groq:** veja [Usar Groq](#usar-groq-cloud-opcional)

### 🐛 "ModuleNotFoundError: No module named 'mtzcode'"

O entry-point `.venv/bin/mtzcode` não funciona porque o cpython do uv não
processa arquivos `.pth` de editable installs. **Use sempre o wrapper
`./bin/mtzcode`** em vez de chamar o binário do venv direto. O wrapper injeta
`PYTHONPATH` e chama `python -m mtzcode`.

### 🐛 "índice não existe em .mtzcode/index.db"

Você tentou usar `search_code` num projeto que ainda não foi indexado.
Solução:

```bash
cd /caminho/do/projeto
mtzcode index
```

Ou dentro do REPL: `/indexar`.

### 🐛 "O modelo chama read('./') e falha"

Sintoma de system prompt ignorado. Acontece mais em modelos Q4 pequenos.
Normalmente os fixes do system prompt já resolvem, mas se persistir:

1. Troque pro modelo maior: `/modelo` → 1 (qwen-14b)
2. Verifique que o arquivo `mtzcode/prompts/system.md` tem a linha
   `Nunca chame \`read\` em um diretório.`
3. Se ainda acontecer, reporte uma issue.

### 🐛 "Erro 400 do Ollama: json: cannot unmarshal object into Go struct"

Bug conhecido do Ollama no endpoint `/v1/chat/completions` quando `tool_calls.arguments`
vem como dict em vez de string. O mtzcode já normaliza isso no `ChatClient`, mas
se aparecer, é porque uma tool call antiga ficou no histórico. Fix: `/limpar`.

### 🐛 "No terminal o Ctrl+C não encerra o servidor"

Uvicorn às vezes demora pra responder ao SIGINT. Tente:
- Esperar 2-3 segundos
- Apertar Ctrl+C de novo
- Se ainda assim não morrer: `lsof -ti:8765 | xargs kill -9`

### 🐛 "Como vejo os logs detalhados do servidor?"

O uvicorn loga tudo no stdout onde você rodou `mtzcode serve`. Pra mais
detalhes, você pode subir direto:

```bash
.venv/bin/python -m uvicorn mtzcode.web.server:create_app --factory --reload --log-level debug
```

O `--reload` também faz o servidor recarregar automaticamente quando você
edita o código — útil pra desenvolvimento.

---

## Configuração (env vars)

Todas opcionais. Coloque no `~/.zshrc` pra persistir.

| Var | Padrão | Descrição |
|---|---|---|
| `MTZCODE_PROFILE` | `qwen-14b` | Perfil de modelo padrão |
| `MTZCODE_TIMEOUT` | `300` | Timeout HTTP em segundos |
| `MTZCODE_EMBED_MODEL` | `nomic-embed-text` | Modelo de embeddings pro RAG |
| `MTZCODE_EMBED_HOST` | `http://localhost:11434` | Host do Ollama pro embedder |
| `MTZCODE_COMMANDS_DIR` | `~/.mtzcode/commands` | Diretório de slash commands customizados |
| `MTZCODE_SKILLS_DIR` | `~/.mtzcode/skills` | Diretório de skills pessoais |
| `MTZCODE_KNOWLEDGE_DIR` | `~/.mtzcode/knowledge` | Diretório das knowledge bases |
| `GROQ_API_KEY` | — | Necessária pro perfil `groq-llama` |

---

## Arquitetura

```
┌─────────────────────────────────────────────────────────────────┐
│  Interface: web (FastAPI + HTML) OU CLI (Typer + Rich)          │
└────────────┬──────────────────────────────────┬─────────────────┘
             │                                  │
             ▼                                  ▼
┌──────────────────────────────┐   ┌──────────────────────────────┐
│  mtzcode.agent.Agent          │   │  slash commands / plan mode  │
│  - run() / run_streaming()    │   │  - load_commands()           │
│  - _consume_stream()          │   │  - parse_slash()             │
│  - _execute_tool_calls()      │   └──────────────────────────────┘
│  - fallback parser de JSON    │
└──────────┬───────────────┬────┘
           │               │
           ▼               ▼
┌─────────────────┐   ┌───────────────────────────────────────────┐
│  ChatClient     │   │  ToolRegistry                              │
│  (OpenAI-compat)│   │  ┌─────────┬─────────┬─────────┬────────┐ │
│  - chat()       │   │  │ read    │ write   │ edit    │ bash   │ │
│  - chat_stream()│   │  │ glob    │ grep    │ search_code*     │ │
└──────┬──────────┘   │  └─────────┴─────────┴─────────┴────────┘ │
       │              └──────────────┬────────────────────────────┘
       ▼                             │
┌──────────────────┐                 ▼
│  HTTP /v1/chat/  │       ┌──────────────────────────┐
│  completions     │       │  mtzcode.rag              │
└──────┬───────────┘       │  - EmbeddingClient        │
       │                   │  - Index (SQLite+numpy)   │
       ▼                   │  - ProjectIndexer         │
┌──────────────────┐       └──────────┬───────────────┘
│  Ollama local    │◄─────────────────┘
│  (porta 11434)   │      (mesma API, via /api/embed)
│                  │
│  - qwen2.5-coder │
│  - nomic-embed   │
└──────────────────┘
```

### Estrutura do código

```
mtzcode/
├── bin/mtzcode              # wrapper bash (PATH-friendly)
├── pyproject.toml
├── LICENSE (MIT)
├── README.md
├── mtzcode/
│   ├── __init__.py
│   ├── __main__.py          # python -m mtzcode
│   ├── cli.py               # Typer + Rich REPL + comandos
│   ├── agent.py             # Agent loop + fallback parser
│   ├── client.py            # ChatClient (OpenAI-compat)
│   ├── config.py            # Config loader
│   ├── profiles.py          # Perfis pré-definidos
│   ├── commands.py          # Slash commands customizados
│   ├── prompts/
│   │   ├── system.md        # System prompt principal
│   │   └── plan_mode.md     # System prompt do modo plano
│   ├── tools/
│   │   ├── base.py          # Tool ABC + ToolRegistry
│   │   ├── read.py          # ReadTool
│   │   ├── write.py         # WriteTool (destructive)
│   │   ├── edit.py          # EditTool (destructive, com diff)
│   │   ├── bash.py          # BashTool (destructive)
│   │   ├── glob.py          # GlobTool
│   │   ├── grep.py          # GrepTool
│   │   └── search.py        # SearchCodeTool (RAG)
│   ├── rag/
│   │   ├── embeddings.py    # EmbeddingClient (Ollama /api/embed)
│   │   ├── index.py         # SQLite + numpy cosine search
│   │   └── indexer.py       # ProjectIndexer (chunking + incremental)
│   └── web/
│       ├── server.py        # FastAPI app + SSE streaming real
│       └── static/
│           └── index.html   # Single-page chat UI
└── tests/
```

---

## Roadmap

- [x] **Fase 0** — Pré-requisitos (Ollama, modelos)
- [x] **Fase 1** — Scaffold + chat REPL
- [x] **Fase 2** — Tools básicas (read, write, edit, bash, glob, grep)
- [x] **Fase 3** — Agent loop com tool calling
- [x] **Profiles** — Múltiplos modelos com `/modelo` (local e cloud)
- [x] **Fase 4** — Streaming de tokens, diffs coloridos, confirmação destrutiva
- [x] **Fase 5** — Slash commands customizados, modo plano
- [x] **Fase 6** — RAG completo (embeddings + índice SQLite + `search_code` tool)
- [x] **Web UI** — FastAPI + HTML single-page estilo app do Claude, streaming real
- [x] **Skills** — sistema de skills plugáveis + skills oficiais
- [x] **Knowledge base** — ingestão de docs (PDF, md, docx) como memória permanente
- [x] **Persistência de histórico** — `~/.mtzcode/logs/*.jsonl`, auto-resume CLI, listagem na Web UI
- [x] **Fila de execução** — várias mensagens enfileiradas enquanto o assistente responde
- [x] **Atalho de voz** — `Cmd/Ctrl+Shift+M`
- [x] **Anexos no chat** — drag & drop / botão de anexar
- [x] **TODO list persistente** — `todo_write` espelhado na aba Tarefas
- [x] **`python_exec`** — sandbox isolado em subprocess
- [x] **Tools GitHub** — clone/info/push/analyze via `gh` CLI
- [x] **Orquestrador fase 1** — `plan_task` + REST `/api/plans/*` + espelho automático na UI
- [x] **Orquestrador fase 2** — `spawn_agent` (sub-agentes com tools isoladas, profundidade limitada)
- [ ] **Orquestrador fase 3** — Verifier (sub-agente que valida cada fase antes de avançar)
- [ ] **RAG por coleção** — `~/.mtzcode/knowledge/<coleção>/` com watcher e drag & drop na UI
- [ ] **Fine-tuning de conversas** — pipeline Evolution API (WhatsApp) + Instagram → train.jsonl
- [ ] **Fase 7** — LoRA fine-tuning sobre logs de uso (especialização)
- [ ] **MCP client** — conectar a servidores Model Context Protocol
- [ ] **Confirmação destrutiva na Web UI** — prompt inline pra write/edit/bash
- [ ] **Multi-sessão na Web UI** — várias conversas em abas
- [ ] **Auto-ping do Ollama** — evitar cold start a cada 5min

---

## Limitações conhecidas

- **Cold start do Ollama** — primeiro request demora ~30s (carregar modelo).
  Depois fica rápido.
- **Modelos Q4 podem alucinar tool calls** — o fallback parser cobre os casos
  comuns, mas modelos menores (7B) são menos confiáveis que os maiores.
- **Sem persistência de histórico** entre sessões — recarregou a página ou
  fechou o terminal, perdeu a conversa.
- **Web UI sem auth** — só use em `localhost`. Não exponha em rede pública.
- **Web UI sem confirmação destrutiva** — no modo CLI o `write`/`edit`/`bash`
  pedem confirmação, na web eles rodam direto. Cuidado ao pedir coisas
  destrutivas via web.
- **Um usuário por vez** — o servidor tem uma única `Session` global com lock.
  Múltiplos usuários simultâneos vão serializar uns aos outros.

## Contribuindo

Issues e PRs são bem-vindos. O projeto é minimalista por design — features
grandes deveriam ser discutidas em issue antes.

Pra desenvolver localmente:

```bash
git clone https://github.com/jpmortaza/mtzcode.git
cd mtzcode
uv venv
uv pip install -e .

# modo dev do servidor (reload automático)
.venv/bin/python -m uvicorn mtzcode.web.server:create_app --factory --reload
```

## Integração WhatsApp (Evolution API)

A pasta `mtzcode/whatsapp/` adiciona um endpoint webhook + cliente Evolution
para conversar com o agente via WhatsApp. Cada contato vira uma sessão isolada
com seu próprio histórico e workspace (`./workspace/<numero>/`).

### Arquitetura

```
Cliente WhatsApp ──► Evolution API ──► POST /whatsapp/webhook
                                            │
                                            ▼
                                    SessionManager (por JID)
                                            │
                                            ▼
                                    Agent loop (mtzcode) ──► tools (read/write/bash/…)
                                            │
                                            ▼
                                    EvolutionClient.send_text() ──► Cliente WhatsApp
```

O webhook devolve `200` em milissegundos; a chamada do agente roda em
background (`asyncio.to_thread`), garantindo que a Evolution não dispare
retry por timeout.

### Variáveis obrigatórias

| Variável | O que é |
|---|---|
| `MTZCODE_PROFILE` | Profile do LLM. `claude-haiku` (recomendado), `claude-sonnet`, `claude-opus`, ou um dos profiles locais (`qwen-14b`, etc). |
| `ANTHROPIC_API_KEY` | Necessária se `MTZCODE_PROFILE` começar com `claude-`. |
| `EVOLUTION_BASE_URL` | Ex: `http://host.docker.internal:8080`. |
| `EVOLUTION_API_KEY` | Header `apikey` da Evolution. |
| `EVOLUTION_INSTANCE` | Nome da instância criada na Evolution. |
| `WHATSAPP_ALLOWED_NUMBERS` | Lista de E.164 sem `+`, separada por vírgula. Vazio = aceita qualquer um (perigoso). |
| `WHATSAPP_WEBHOOK_SECRET` | Opcional. Se setado, exige header `x-webhook-secret` no POST. |
| `WHATSAPP_AUTO_CONFIRM` | `true` para deixar o agente rodar tools destrutivas (write/bash) sem confirmação. Default `false` → tools destrutivas são bloqueadas. |

### Endpoints

- `POST /whatsapp/webhook` — recebe eventos `messages.upsert` da Evolution.
- `POST /whatsapp/send` — envia mensagem manualmente (`{"number":"...","text":"..."}`).
- `POST /whatsapp/reset/{jid}` — apaga histórico de um contato.
- `GET  /whatsapp/health` — diagnóstico (instância, sessões ativas, profile).

### Comandos do bot

| Mensagem | Efeito |
|---|---|
| `/reset` | Zera histórico desta conversa |
| `/help` | Mostra menu |
| qualquer outro texto | Vai pro agente |

### Segurança

- **Sempre** configure `WHATSAPP_ALLOWED_NUMBERS` em produção.
- **Sempre** configure `WHATSAPP_WEBHOOK_SECRET` (o mesmo valor configurado
  na Evolution na hora de cadastrar o webhook).
- O agente respondendo no WhatsApp tem acesso aos mesmos tools do CLI/web
  (`read`, `write`, `bash`, etc). Mantenha `WHATSAPP_AUTO_CONFIRM=false`
  até estar confiante na configuração.
- Cada contato roda em seu workspace separado (`workspace/<digitos>`),
  o que dá isolamento básico — mas o tool `bash` pode sair de lá se
  o modelo decidir. Use `WHATSAPP_AUTO_CONFIRM=false` como rede de
  segurança.

### Trocar entre Claude e Ollama

A factory `make_client` em `mtzcode/client.py` escolhe o cliente certo pelo
campo `backend` do profile (`anthropic` → `AnthropicClient`, qualquer outro
→ `ChatClient` OpenAI-compatible). Pra trocar de modelo em runtime, use o
endpoint `/api/profile` do web UI ou edite `MTZCODE_PROFILE` no `.env` e
reinicie.

## Licença

[MIT](LICENSE) — faça o que quiser.

---

**Construído por [Jean Mortaza](https://github.com/jpmortaza).**
