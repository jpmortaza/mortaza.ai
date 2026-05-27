# Skills do mtzcode

Skills são **prompts especializados** que redefinem o comportamento do agent
pra uma tarefa ou domínio específico. Quando você ativa uma skill com
`/skill <nome>`, o system prompt do agent é trocado, fazendo ele agir como
um especialista naquela área.

## Como usar

No REPL do mtzcode:

```
você › /skill
(lista as skills disponíveis)

você › /skill python-expert
✓ skill ativada: python-expert

você › /skill off
✓ skill desativada (voltou ao prompt padrão)
```

## Skills oficiais

As skills deste diretório vêm junto com o mtzcode. Elas são carregadas
automaticamente ao iniciar o REPL.

| Skill | Descrição |
|---|---|
| [`python-expert`](python-expert/) | Especialista em Python moderno (3.12+), PEP 8, type hints, async |
| [`javascript-expert`](javascript-expert/) | Especialista em JavaScript/TypeScript moderno, Node, browser |
| [`sql-dba`](sql-dba/) | DBA: escreve, otimiza e revisa queries SQL, explica EXPLAIN plans |
| [`code-reviewer`](code-reviewer/) | Faz code review crítico com foco em bugs, segurança e legibilidade |
| [`git-helper`](git-helper/) | Ajuda com comandos git, resolve conflitos, explica histórico |
| [`writer-pt`](writer-pt/) | Escreve e revisa textos em português brasileiro (docs, READMEs) |

## Como criar sua própria skill

### 1. Skills pessoais (não compartilhadas)

Crie um diretório em `~/.mtzcode/skills/<nome-da-skill>/` com um arquivo `SKILL.md`:

```bash
mkdir -p ~/.mtzcode/skills/meu-especialista
cat > ~/.mtzcode/skills/meu-especialista/SKILL.md <<'EOF'
---
name: meu-especialista
description: Especialista em [domínio]
author: seu-nome
version: 1
---

# Meu Especialista

Você é um especialista em [X]. Siga estas regras ao trabalhar:

1. ...
2. ...
3. ...

## Conhecimento específico

...

## Estilo de resposta

- Sempre em português
- Seja conciso
- Mostre exemplos práticos
EOF
```

Recarregue o REPL e a skill aparece em `/skill`.

### 2. Skills oficiais (contribuir pro repo)

1. Fork este repositório
2. Crie um diretório `skills/<nome-da-sua-skill>/`
3. Adicione um `SKILL.md` seguindo o formato abaixo
4. Abra um Pull Request

Skills aceitas no repo oficial precisam:

- Ter um propósito claro e bem definido
- Prompt em **português brasileiro**
- Seguir o formato `SKILL.md` documentado abaixo
- Não duplicar funcionalidade de skills existentes

## Formato do `SKILL.md`

```markdown
---
name: nome-da-skill          # ID único, kebab-case
description: Descrição curta  # ~1 linha, aparece na lista /skill
author: seu-github            # opcional
version: 1                    # opcional
tools: [read, grep, glob]     # opcional — restringe tools permitidas
---

# Título da Skill

Corpo em markdown. Este é o **system prompt** que será usado quando a skill
estiver ativa. Escreva como se estivesse instruindo um especialista:

- Regras que ele deve seguir
- Convenções do domínio
- Estilo de resposta esperado
- Exemplos (quando útil)

Você pode ser detalhado — system prompts longos funcionam bem desde que
sejam focados e bem estruturados.
```

### Campos do frontmatter

| Campo | Obrigatório | Descrição |
|---|---|---|
| `name` | Sim | ID único da skill (kebab-case, sem espaços) |
| `description` | Sim | Frase curta descrevendo o que a skill faz |
| `author` | Não | Seu usuário do GitHub ou nome |
| `version` | Não | Versão semântica simples (1, 2, 1.1, etc) |
| `tools` | Não | Lista de tools permitidas. Vazio = todas. |

### Dicas pra escrever bons prompts

- **Seja específico.** "Siga PEP 8" é melhor que "use boas práticas".
- **Diga o que NÃO fazer.** Modelos pequenos precisam de limites claros.
- **Dê exemplos.** 2-3 exemplos valem mais que 2 páginas de descrição.
- **Defina o estilo.** Tom, nível de detalhe, formato de resposta.
- **Use a linguagem do domínio.** Jargão técnico específico ajuda o modelo
  a entrar no contexto.

## Skills da comunidade

Se você criou uma skill útil e quer compartilhar:

1. Fork + PR (pra entrar no repo oficial) **ou**
2. Publique num gist/repo próprio e compartilhe no README
