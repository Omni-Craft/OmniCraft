# OmniCraft for VS Code

Uma extensão minimalista de VS Code que abre o seu **servidor OmniCraft local em
execução** dentro do editor — um painel ao lado do editor que traz num iframe a
mesma UI que você vê em `http://127.0.0.1:6767`. É um cliente leve da API HTTP
já existente do servidor local (`server/API.md`); não há nada novo para rodar
do lado do servidor.

Esta é a primeira contribuição, deliberadamente pequena (acompanhe em
[omnicraft-ai/omnicraft#1219](https://github.com/omnicraft-ai/omnicraft/issues/1219)):
descoberta de localhost, o painel de iframe do editor, e os ícones da barra de
atividades / título do editor. Sessões, diffs, enviar seleção, e renderização
remota/embutida ficam intencionalmente fora de escopo por enquanto.

## Como funciona

- Na ativação, a extensão descobre um servidor local em execução via
  `~/.omnicraft/local_server.pid` e uma checagem em `/health` (ou usa
  `omnicraft.serverUrl` quando definido para uma URL de localhost).
- A view da barra de atividades do OmniCraft oferece um botão **Open OmniCraft**.
  O comando **OmniCraft: Open** (`omnicraft.open`) — também na barra de título do
  editor e na paleta de comandos — abre um painel ao lado do editor que enquadra
  o servidor em execução.
- O caminho por iframe é usado apenas para servidores **locais**; um servidor
  local é loopback e não precisa de autenticação, então nenhum token aparece na
  URL do iframe.

## Configurações

| Configuração | Padrão | Finalidade |
|---|---|---|
| `omnicraft.serverUrl` | `""` | Sobrescrita manual da URL do servidor **localhost** (ex.: `http://127.0.0.1:6767`); vazio = descoberta automática. URLs que não sejam localhost não são suportadas nesta versão. |

## Limitação conhecida

No macOS, o VS Code não entrega as teclas `Cmd+A/C/V` para um iframe de origem
cruzada dentro de um webview, então colar pelo teclado nos campos do app
enquadrado não funciona ali. É um problema do próprio VS Code, não corrigível
pela extensão no caminho de renderização por iframe — veja microsoft/vscode#129178
e microsoft/vscode#182642. (Os botões de copiar na própria página e os caminhos
via `navigator.clipboard` continuam funcionando.)

## Build / teste / empacotamento

```bash
npm ci
npm run type-check   # tsc --noEmit
npm run test         # vitest run
npm run build        # esbuild -> dist/extension.js
npm run package      # @vscode/vsce package -> omnicraft-vscode-<version>.vsix
```

Instale o `.vsix` resultante pela view Extensions → "Install from VSIX…". O
runtime do `.vsix` é `dist/extension.js` + `media/`.

## Estrutura

```
src/
├── extension.ts        # activate()/deactivate() — conecta descoberta + painel + comando + view
├── commands/openPanel.ts  # o comando omnicraft.open
├── panel/              # EditorPanelController, host.ts (render), iframeHtml.ts, csp.ts
├── config/             # configurações + resolução do alvo do servidor localhost
└── discovery/          # descoberta do servidor local (pidfile / health / liveness)
```

Licenciado sob Apache-2.0 (veja `LICENSE`). Contribuições exigem assinatura DCO
(`git commit -s`), conforme o `CONTRIBUTING.md` do repositório.
