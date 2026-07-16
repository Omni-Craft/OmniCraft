# web

A web UI para `omnicraft server --agent <agent>`. SPA construída com Vite +
React + TypeScript + Tailwind v4 + shadcn/ui. Conversa com a superfície da
API atual do OmniCraft (`/v1/agents`, `/v1/sessions`,
`/v1/sessions/{id}/resources/files` por sessão).

## Desenvolver

Num terminal, suba o servidor omnicraft (porta padrão `6767`). Use
`--agent` para pré-registrar um ou mais agentes na inicialização (aceita um
arquivo YAML ou um diretório de imagem de agente; pode repetir):

```bash
.venv/bin/omnicraft server --agent examples/hello_world.yaml
```

Em outro terminal, suba o servidor de dev do Vite (porta `5173`):

```bash
cd web
npm install
npm run dev
```

O servidor de dev do Vite faz proxy de `/v1` e `/api` para
`http://localhost:6767`. Defina `OMNICRAFT_URL` para sobrescrever o alvo do
proxy:

```bash
OMNICRAFT_URL=http://localhost:9000 npm run dev
```

Opções adicionais do `omnicraft server`:

| Flag                  | Padrão                 | Descrição                                     |
| --------------------- | ---------------------- | --------------------------------------------- |
| `--host`              | `127.0.0.1`            | Host para vincular                            |
| `-p` / `--port`       | `6767`                 | Porta para escutar                            |
| `--database-uri`      | `<data-dir>/chat.db`   | URI do banco de dados para os stores          |
| `--artifact-location` | `<data-dir>/artifacts` | Caminho para armazenamento de artefato        |
| `-c` / `--config`     | (nenhum)               | Caminho do arquivo de config YAML             |
| `--execution-timeout` | `7200`                 | Máximo de segundos de wall-clock por execução |
| `--agent`             | (nenhum)               | Pré-registra um agente (repetível)            |

## Build + servir pelo servidor OmniCraft

```bash
cd web
npm run build
```

O Vite escreve o bundle em `../omnicraft/server/static/web-ui/` (configurado
em `vite.config.ts`). Quando esse diretório existe e contém `index.html`, o
app FastAPI em `omnicraft/server/app.py` o monta em `/`. Depois de um build:

```bash
.venv/bin/omnicraft server --agent examples/hello_world.yaml
# abra http://localhost:6767/
```

## Lint + format

```bash
npm run lint          # oxlint .
npm run lint:fix      # oxlint --fix .
npm run format        # prettier --write .
npm run format:check  # prettier --check .
npm run type-check    # tsc -b
```

`npm run type-check` roda na CI como parte do job `Pre-commit checks`
(`.github/workflows/lint.yml`) e bloqueia o merge. Rode localmente antes de
commitar qualquer mudança em `web/`.

## Teste

```bash
npm run test          # vitest run
npm run test:watch    # vitest em modo watch
```

## Paridade do reducer

O reducer TypeScript em `src/lib/blockStream.ts` é um espelho manual do
reducer Python em `sdks/python-client/omnicraft_client/_stream.py`. O mesmo
vale para:

| Arquivo TS                    | Espelha                                            |
| ----------------------------- | -------------------------------------------------- |
| `src/lib/blocks.ts`           | `omnicraft_client/_blocks.py`                      |
| `src/lib/events.ts`           | `omnicraft_client/_events.py`                      |
| `src/lib/types.ts`            | subconjunto mínimo de `omnicraft_client/_types.py` |
| `src/lib/sse.ts`              | `omnicraft_client/_sse.py`                         |
| `src/lib/blockStream.ts`      | `omnicraft_client/_stream.py`                      |
| `src/lib/blockStream.test.ts` | `tests/frontends/sdk/test_stream.py`               |

Hoje **não existe gate de CI cross-language**. Quando `_stream.py` muda por
um bug real (ex.: uma peculiaridade nova de harness, um caso de borda de
dedup), a porta TypeScript pode ficar defasada — a divergência só aparece
quando alguém roda `npm run test` depois de uma mudança de comportamento.
Fluxo de trabalho quando `_stream.py` muda:

1. Leia o diff de `_stream.py` (ou `_blocks.py` / `_events.py`).
2. Atualize `blockStream.ts` (ou `blocks.ts` / `events.ts`) para
   corresponder.
3. Adicione ou atualize um caso em `blockStream.test.ts` que fixe o novo
   comportamento — no mesmo formato de `test_stream.py`.
4. `npm run test` → verde.

Se algum dia decidirmos que a paridade de fixture cross-language vale a
manutenção, portaríamos a abordagem de fixture capturada usada em
`test_stream.py`.

### Divergências exclusivas da web

A web carrega algumas construções que o SDK Python não tem, de propósito.
Elas estão listadas aqui para que um futuro mantenedor não tente "restaurar
a paridade" espelhando-as também.

- `UserMessageBlock` (em `blocks.ts`) — expõe itens de mensagem do usuário
  persistidos como blocos, para que o walker de bolhas veja uma lista única
  e plana. O `BlockStream.stream()` do SDK nunca emite mensagens do usuário
  (os consumidores dele recebem o input do usuário como argumento próprio
  do chamador, não de volta pelo stream).
- `BlockContext.responseId` + `BlockContext.itemId` — populados pelo reducer
  TS a partir do formato de fio SSE (`response.created.response.id` e
  `event.item.id` / `event.item.response_id` em `output_item.done`) para que
  cada bloco saiba a sua origem no servidor. Os eventos TS `ToolCall` /
  `ToolResult` / `MessageDone` / `NativeToolCall` carregam `itemId` +
  `responseId` para passar os valores adiante.
- Armazenamento plano de blocos em `chatStore.blocks`, agrupado no momento
  da renderização por `buildBubbles`, indexado por `ctx.responseId`. O SDK
  não tem equivalente — os consumidores dele iteram o block stream de forma
  procedural, sem um store com estado.

Quando `_stream.py` / `_events.py` / `_blocks.py` mudam por um motivo
substantivo (tipo de evento novo, caso de borda de dedup novo), continue
espelhando as mudanças de _comportamento_ aqui; só deixe as divergências
acima como estão.

## Stack

- Vite + React 19 + TypeScript
- Tailwind v4 (`@import "tailwindcss"`, sem arquivo de config)
- shadcn/ui (preset `radix-nova`, base neutra, variáveis CSS)
- TanStack Query, Zustand, React Router v7
- streamdown (+ `@streamdown/code`, `@streamdown/math`, `@streamdown/mermaid`),
  shiki, framer-motion, cmdk, react-hotkeys-hook, use-stick-to-bottom,
  next-themes, react-hook-form, zod
- Lint: oxlint. Format: prettier.
