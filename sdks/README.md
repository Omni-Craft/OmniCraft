# SDKs

Pacotes Python para integração com o omnicraft.

## Estrutura

```
sdks/
  python-client/           # Cliente HTTP/SSE headless
    pyproject.toml
    omnicraft_client/    # import omnicraft_client
  ui/                      # Camada de UI de terminal (Rich + prompt_toolkit)
    pyproject.toml
    omnicraft_ui_sdk/    # import omnicraft_ui_sdk
      terminal/
```

As skills do Claude Code para desenvolvimento de SDK ficam em `.claude/skills/`.

## `omnicraft_client` — o cliente headless

Cliente HTTP/SSE puro. Sem Rich, sem prompt_toolkit, sem dependências
de terminal. Use isto para:

- Scripts que invocam um agente e coletam a saída.
- Frontends web, bots de Slack, harnesses de teste — qualquer coisa que não seja terminal.
- Como camada de fundação para o `omnicraft_ui_sdk` abaixo.

Três níveis de abstração estão disponíveis:

1. **Eventos brutos** — `session.send()` produz eventos wire tipados
   (`ResponseCreated`, `TextDelta`, `ToolCallDone`, etc.). 1:1 com o SSE.
2. **Blocos semânticos** — `BlockStream` agrega os eventos em unidades de
   nível mais alto (`TextChunk`, `ToolGroup`, `ReasoningBlock`, …). Frameworks
   que consomem esses blocos não precisam reimplementar a máquina de estados
   do stream.
3. **Transformações componíveis** — `pipe`, `skip_blocks`,
   `skip_intermediate_ends`, `merge_text_across_iterations`, `only_agent`.

### Instalação

```bash
pip install -e sdks/python-client
```

### Invocação mínima

```python
import asyncio
from omnicraft_client import OmniCraftClient

async def main():
    async with OmniCraftClient(base_url="http://localhost:8080") as client:
        session = client.session(model="archer")
        async for event in session.send("hello"):
            print(event)

asyncio.run(main())
```

### Usando blocos semânticos (web, Slack, ou qualquer UI própria)

```python
from omnicraft_client import (
    BlockStream, TextChunk, ToolGroup, ResponseEndBlock,
    pipe, skip_intermediate_ends,
)

async def handle(websocket, session, text):
    block_stream = BlockStream()
    async for block in pipe(
        block_stream.stream(session, text),
        skip_intermediate_ends(),
    ):
        match block:
            case TextChunk(text=t):
                await websocket.send_json({"type": "text", "chunk": t})
            case ToolGroup(executions=execs):
                await websocket.send_json({"type": "tools", "data": [
                    {"name": e.name, "output": e.output} for e in execs
                ]})
            case ResponseEndBlock(status=s):
                await websocket.send_json({"type": "done", "status": s})
```

## `omnicraft_ui_sdk` — o frontend de terminal

Camada leve sobre o `omnicraft_client` para construir REPLs de terminal.
Fornece:

- **RichBlockFormatter** — converte valores `StreamBlock` em renderizáveis
  do Rich. Faça subclasse e sobrescreva um método para customizar.
- **TerminalHost** — gerencia o prompt_toolkit: barra de entrada fixa,
  streaming em segundo plano, Escape para cancelar, histórico persistente.

### Instalação

```bash
pip install -e sdks/ui
```

(Traz o `omnicraft-client` como dependência.)

### REPL mínimo

```python
import asyncio
from omnicraft_client import (
    OmniCraftClient, LocalServer, BlockStream,
    pipe, skip_intermediate_ends,
)
from omnicraft_ui_sdk import RichBlockFormatter, TerminalHost

async def main():
    async with LocalServer(agent_path="./my-agent/") as server:
        client = server.client
        session = client.session(model="my-agent")
        block_stream = BlockStream()
        fmt = RichBlockFormatter()
        host = TerminalHost(model_name="my agent")

        async def on_input(text):
            host.output(fmt.user_message(text))
            async for block in pipe(
                block_stream.stream(session, text),
                skip_intermediate_ends(),
            ):
                for item in fmt.format(block):
                    host.output(item)
                await asyncio.sleep(0)

        async with host:
            host.output(fmt.welcome("my agent"))
            await host.run(on_input)

asyncio.run(main())
```

### Customização

Sobrescreva um método do formatador:

```python
class MyFormatter(RichBlockFormatter):
    def format_tool_group(self, block):
        from rich.tree import Tree
        tree = Tree("Tools")
        for ex in block.executions:
            tree.add(f"{ex.name} → {(ex.output or '')[:50]}")
        return [tree]
```

Use transformações para remodelar o stream de blocos:

```python
from omnicraft_client import pipe, skip_blocks, ReasoningBlock

stream = pipe(
    block_stream.stream(session, text),
    skip_blocks(ReasoningBlock),  # Esconde o raciocínio
)
```

## Implementação de referência

O REPL embutido em `omnicraft/repl/` demonstra todas as funcionalidades:
streaming, chamadas de ferramenta, raciocínio, comandos de barra, troca de
conversa, cronômetro decorrido. Veja `omnicraft/repl/_repl.py`.
