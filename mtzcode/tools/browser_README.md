# BrowserTool

Tool do mtzcode que controla um navegador real (Chromium) via [Playwright].
Mantém uma sessão única entre chamadas sucessivas, então cookies, login e a
página atual são preservados ao longo de uma conversa.

## Instalação

```bash
pip install playwright
playwright install chromium
```

Sem isso, ao usar a tool você recebe uma mensagem pedindo pra rodar os comandos
acima — o módulo não quebra na importação, só na execução.

## Quando usar

Use `browser` para tarefas web **interativas**:

- login em sites
- preencher e submeter formulários
- scraping de páginas que dependem de JS
- automação de fluxos multi-step

Para leitura simples de uma URL estática, prefira `web_fetch` que é muito mais
rápido e não sobe um Chromium.

## Ações suportadas

| action       | args necessários            | retorno                              |
| ------------ | --------------------------- | ------------------------------------ |
| `navigate`   | `url`                       | "navegou para URL, título: X"        |
| `click`      | `selector`                  | "clicou em {selector}"               |
| `type`       | `selector`, `text`          | "digitou em {selector}"              |
| `screenshot` | —                           | caminho do PNG em `/tmp`             |
| `eval`       | `script`                    | resultado do JS (str)                |
| `text`       | —                           | `inner_text('body')` (até 4000 chars)|
| `wait`       | `selector`                  | "selector X apareceu"                |
| `back`       | —                           | "voltou para: URL"                   |
| `forward`    | —                           | "avançou para: URL"                  |

`timeout_ms` (default 10000) controla esperas e navegação.
`headless` (default `False`) só vale na **primeira** chamada — depois disso o
browser já está aberto e o flag é ignorado.

## Exemplos

Navegar e tirar screenshot:

```json
{"action": "navigate", "url": "https://example.com"}
{"action": "screenshot"}
```

Login simples:

```json
{"action": "navigate", "url": "https://app.exemplo.com/login"}
{"action": "type", "selector": "#email", "text": "eu@exemplo.com"}
{"action": "type", "selector": "#senha", "text": "secret"}
{"action": "click", "selector": "button[type=submit]"}
{"action": "wait",  "selector": ".dashboard"}
```

Extrair dados via JS:

```json
{"action": "eval", "script": "document.querySelectorAll('h2').length"}
```

## Notas

- A tool é marcada como `destructive=True` porque pode submeter forms reais.
- O Chromium é fechado automaticamente via `atexit` quando o mtzcode encerra.
- Para resetar a sessão (logout, novo perfil), encerre o mtzcode e abra de novo.

[Playwright]: https://playwright.dev/python/
