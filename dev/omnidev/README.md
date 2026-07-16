# omnidev

Ferramental de desenvolvimento para o OmniCraft, num binário só, com duas
capacidades independentes:

1. Um **supervisor de pod** de dev por repositório (`omnidev` puro) — o
   padrão.
2. **Gerenciamento de instalação** (`omnidev install`/`update`/`check`) —
   instala e mantém atualizado um omnicraft baseado em git. Veja
   [Gerenciando a sua instalação do omnicraft](#gerenciando-a-sua-instalação-do-omnicraft).
   Esses subcomandos não precisam de checkout e rodam de qualquer lugar.

## Supervisor de pod

Um supervisor de **pod** de dev por repositório, como uma única interface de
terminal de longa duração. Ele substitui o fluxo de dev local de três
terminais (`omnicraft server`, `omnicraft host`, `npm run dev`) por um
processo só que:

- roda cada checkout num **pod isolado** — com seu próprio diretório de
  estado, banco de dados, artefatos, logs, e portas auto-alocadas — para que
  vários worktrees nunca colidam;
- **supervisiona** o servidor backend, o daemon de host, e o frontend Vite,
  reiniciando qualquer um que trave (com backoff);
- **recarrega o backend** (server → host) quando você edita
  `omnicraft/**/*.py`; o frontend se recarrega sozinho via Vite HMR;
- te dá **painéis de log roláveis por processo**, além de uma visão combinada.

## Build e execução

Requer os pré-requisitos de dev normais do repositório (`uv` para Python,
`npm` para a web UI) mais um toolchain Rust.

```bash
cd dev/omnidev
cargo run            # lança a TUI para o checkout ao redor
```

Rode de qualquer lugar dentro do checkout — ele sobe na árvore até a raiz do
repositório (o marcador `.jj`/`.git`) e exige que `omnicraft/` e `web/`
estejam presentes. Construa um binário de release com
`cargo build --release` (fica em `target/release/omnidev`).

## O que ele inicia

| Processo | Comando | Notas |
|---|---|---|
| server | `uv run omnicraft server --host 127.0.0.1 --port <p> --database-uri … --artifact-location …` | Aguardado via `GET /health`. |
| host   | `uv run omnicraft host --server http://127.0.0.1:<p>` | Iniciado assim que o servidor fica saudável. |
| vite   | `npm run dev -- --host <host> --port <p> --strictPort` (cwd `web/`) | `OMNICRAFT_URL` aponta o proxy dele para o servidor do pod. |

Antes do Vite iniciar (e num restart manual do Vite), o omnidev roda
`npm install` em `web/` quando necessário — `node_modules/` está faltando, ou
`package.json` / `package-lock.json` é mais novo do que ele — para que um
checkout novo ou uma dependência nova não façam o Vite falhar no scan de
dependências. A saída é transmitida para o painel `vite`.

Abra a UI na URL `ui` mostrada no cabeçalho (o servidor de dev do Vite).

## Isolamento

Só o estado próprio do OmniCraft é isolado por pod — o suficiente para que
pods concorrentes nunca compartilhem um banco de dados, pidfile de servidor,
ou `config.yaml` — via `OMNICRAFT_DATA_DIR`, `OMNICRAFT_DATABASE_URI`,
`OMNICRAFT_URL`, e `OMNICRAFT_CONFIG_HOME`. Todo o resto (o seu `HOME` real,
credenciais, e caches do uv/npm) é herdado, porque os agentes que o OmniCraft
roda precisam disso. Isso é deliberadamente mais leve do que o sandbox
hermético do `scripts/backend-smoke.sh`, que reaponta `HOME`/`XDG_*` para não
tocar em nada real.

Cada pod ganha o seu próprio `config.yaml` em `<pod>/config/`, apontado por
`OMNICRAFT_CONFIG_HOME`. Na primeira criação ele é **semeado** a partir do seu
`~/.omnicraft/config.yaml` real (se presente), para que o pod funcione de
cara — ele mantém os seus provedores — depois disso os dois são
independentes: edições de configuração do servidor dentro de um pod (pela UI
ou `omnicraft config`) não tocam na sua configuração real. `--clean` apaga o
diretório do pod, então a próxima execução re-semeia a partir da sua
configuração real.

O diretório do pod tem como padrão
`${XDG_CACHE_HOME:-~/.cache}/omnidev/<repo-name>-<hash>/`, chaveado pelo
caminho canônico do checkout. Os logs por processo são gravados também em
`<pod>/logs/{server,host,vite}.log` para inspeção fora da TUI.

## Opções

```
--server-port <N>   Force the backend port (default: probe from 6767)
--vite-port <N>     Force the Vite port (default: probe from 5173)
--vite-host <ADDR>  Vite bind host (default: 127.0.0.1; use 0.0.0.0 for LAN access)
--trust-lan-origins Trust this machine's LAN origins (for device testing)
--pod-dir <PATH>    Use a specific pod dir instead of the per-repo default
--no-vite           Backend + host only (no frontend)
--clean             Wipe the pod dir before starting
```

`--vite-host 0.0.0.0` expõe o servidor de dev do Vite em todas as interfaces
para teste em dispositivo. O Vite continua fazendo proxy do tráfego de API
para o backend do pod via `127.0.0.1`.

### Testando a partir de um celular ou tablet

`--vite-host 0.0.0.0` sozinho deixa um dispositivo carregar a UI, mas o
backend roda no modo local de usuário único, onde o guard de CSRF/CSWSH dele
confia só em origens loopback. Um dispositivo carrega a UI em
`http://<your-lan-ip>:<vite-port>`, então o navegador dele carimba essa
origem non-loopback em toda requisição — e o guard então rejeita uploads
multipart (403) e recusa o stream de WebSocket ao vivo.

`--trust-lan-origins` resolve isso: o omnidev enumera os endereços IPv4 de LAN
desta máquina e confia nas origens `http://<ip>:<vite-port>` correspondentes
via allowlist `OMNICRAFT_WS_ALLOWED_ORIGINS` do servidor (mesclada com
qualquer valor que você já tenha exportado). Ela continua exata — só essas
origens são confiadas, nada é desativado — então serve para pods de dev, não
para servidores publicados. As origens confiadas são impressas no log
combinado na inicialização.

```bash
omnidev --vite-host 0.0.0.0 --trust-lan-origins
```

Isso cobre endereços IPv4 de LAN; hostnames mDNS `.local` e origens HTTPS não
são confiados automaticamente (adicione-os você mesmo a
`OMNICRAFT_WS_ALLOWED_ORIGINS`).

## Teclas

| Tecla | Ação |
|---|---|
| `1` / `2` / `3` / `0` | Foca o painel server / host / vite / combinado |
| `Tab` | Circula entre os painéis |
| `↑` `↓` `PgUp` `PgDn` | Rola (destaca do tail) |
| `f` | Alterna o seguir-tail |
| `r` | Reinicia o processo focado (server/host reiniciam como um par) |
| `R` | Reinicia o backend (server depois host) |
| `c` | Limpa o painel focado |
| `q` / `Ctrl-C` | Sai e derruba todos os processos |

## Gerenciando a sua instalação do omnicraft

Para quem *roda* o omnicraft (instalado via git com `uv tool install`) em vez
de desenvolvê-lo. Isso encapsula a sintaxe cheia de detalhes do PEP 508 de
instalação e adiciona uma checagem diária de atualização — preenchendo uma
lacuna, já que o próprio aviso de atualização do omnicraft só funciona para
instalações via wheel do PyPI e ignora instalações via git.

Esses subcomandos gerenciam a ferramenta global e funcionam a partir de
**qualquer diretório** (sem checkout necessário).

```
omnidev install     # uv tool install omnicraft a partir do git (extra databricks, main)
omnidev update      # reinstala a versão mais recente da ref/extras rastreada
omnidev check       # checa por uma atualização; pergunta se quer atualizar num TTY
omnidev refresh     # atualiza o cache de checagem a partir da rede (geralmente destacado)
omnidev shell-hook  # imprime o trecho de checagem diária para o rc do seu shell
```

Opções do `install`: `--ref <branch/tag/sha>` (padrão `main`),
`--extra <name>` (repetível; padrão `databricks`), `--no-default-extra`
(instala sem extras), `--repo <url>`. A escolha é salva em
`${XDG_CONFIG_HOME:-~/.config}/omnidev/install.toml`, então o `update`
reaproveita ela.

Instalar via git **constrói a web UI a partir do código-fonte**, então o Node
22+/npm precisam estar no PATH (o wheel do PyPI já traz a UI pré-construída;
a instalação via git não). O `omnidev install` falha cedo com uma mensagem
clara se `uv` ou `npm` estiverem faltando.

### Checagem diária de atualização

Adicione o hook ao rc do seu shell uma vez para ser avisado, no máximo uma vez
por dia, quando um commit novo da `main` estiver disponível — e ser oferecida
a chance de atualizar na hora:

```bash
omnidev shell-hook >> ~/.zshrc     # ou ~/.bashrc
```

O trecho em si se protege com `command -v omnidev`, então é um no-op em
shells onde o omnidev não está no PATH — nada para falhar. (Adicionar o
trecho é preferível a `eval "$(omnidev shell-hook)"`: o segundo rodaria o
omnidev a cada início de shell e imprimiria um erro "command not found"
sempre que o omnidev estivesse ausente.)

A cada shell interativo ele roda `omnidev check --quiet`, que lê um resultado
em cache (`${XDG_CACHE_HOME:-~/.cache}/omnidev/omnicraft-check.json`) e,
quando desatualizado (>24h), o atualiza num processo em background destacado
— então o início do shell nunca bloqueia na rede. Quando um commit mais novo
está disponível ele imprime um aviso e, num terminal, pergunta
`Update omnicraft now? [y/N]`; ao responder sim, ele roda `omnidev update` em
primeiro plano. Recusar suprime esse mesmo commit até que um mais novo
apareça. Defina `OMNICRAFT_NO_UPDATE_CHECK` no seu ambiente se quiser
silenciar o aviso separado do próprio omnicraft.
