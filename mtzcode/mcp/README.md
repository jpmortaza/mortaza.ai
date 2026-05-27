# Suporte a MCP no mtzcode

Este pacote conecta o mtzcode a servidores [MCP (Model Context Protocol)](https://modelcontextprotocol.io)
externos — Gmail, Notion, GitHub, filesystem etc.

## Configuração

1. Instale o SDK oficial: `pip install mcp`
2. Crie o arquivo `~/.mtzcode/mcp_servers.json` no mesmo formato do
   Claude Desktop. Veja `example_config.json` neste diretório como modelo.
3. Cada servidor declara `command`, `args` e (opcionalmente) `env`. O
   transporte usado é stdio (o mais comum entre servidores MCP).

## Como funciona

- `MCPManager` carrega o JSON, conecta a todos os servidores habilitados
  e mantém as sessões abertas em um event loop dedicado.
- `MCPToolBridge` envolve cada tool MCP em uma `Tool` nativa do mtzcode,
  prefixando o nome com `mcp_<servidor>_<tool>`.
- `register_mcp_tools(registry, manager)` registra tudo no `ToolRegistry`
  para o agente principal usar normalmente via tool calling.

## Observações

- Tools MCP são marcadas como `destructive=True` por padrão (efeitos
  colaterais desconhecidos) — confirmação será pedida antes de executar.
- Se o pacote `mcp` não estiver instalado, o `MCPManager` opera em modo
  stub: loga um aviso mas não quebra o resto do mtzcode.
