# Publicando o OmniCraft

O OmniCraft traz vários jeitos de publicar o servidor, organizados por
plataforma de destino. Escolha o que combina com o seu ambiente.

Publicar te dá uma URL estável: as sessões passam a ser acessíveis de qualquer
dispositivo, inclusive do celular (a web UI foi feita para mobile), e colegas
podem entrar. O servidor é o ponto de coordenação; o seu código e as suas
chaves de modelo ficam nas máquinas que se registram como hosts (veja
[Modelo de execução](#modelo-de-execução)).

## Publicar em um clique

Sem nenhuma ferramenta local. Escolha uma plataforma, clique no botão, e o seu
servidor OmniCraft está no ar com HTTPS em poucos minutos.

| Plataforma | Botão | Documentação |
|---|---|---|
| **Render** | [![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/omnicraft-ai/omnicraft) | [`render/README.md`](render/README.md) |
| **Railway** | *(botão pendente; veja abaixo)* | [`railway/README.md`](railway/README.md) |

<!-- TODO(oss-release): publicar o template do Railway em railway.com/new/template
     quando o repositório for público, e então trocar a linha do Railway acima por:
     [![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/<template-id>)
     Passos: railway.com/new/template → apontar para o repo público → adicionar o
     plugin de Postgres → publicar → copiar a URL de deploy → atualizar este
     arquivo e o deploy/railway/README.md. -->

As duas provisionam um banco Postgres gerenciado automaticamente e usam por
padrão o provedor de autenticação embutido `accounts`, então um deploy novo já
nasce multiusuário, sem IdP externo. No primeiro boot um admin é criado
automaticamente (a senha aparece nos logs do serviço); convide seus colegas
pela web UI. Prefere o seu próprio IdP? Troque para OIDC depois do deploy
definindo as variáveis `OMNICRAFT_OIDC_*` (a autenticação continua ligada; é o
issuer que vira a chave); veja o README da plataforma para os dois caminhos.

**Mais três plataformas** são suportadas com um pouco mais de configuração (não
é um botão só): **Fly.io** (`fly deploy`, ou o Launch da web UI dele),
**Hugging Face Spaces** (um Docker Space no nível de demo) e **Modal**
(`modal deploy`, um servidor web sempre no ar com um Volume durável para
artefatos). Veja o menu abaixo. Fly e HF Spaces podem rodar no **nível leve com
SQLite**, sem banco nenhum para provisionar (veja
[Banco de dados: Postgres ou SQLite](#banco-de-dados-postgres-ou-sqlite)); o
Modal precisa de um Postgres seu.

---

```
deploy/
├── README.md          ← (este arquivo) o menu
│
├── render/            ← deploy de 1 clique na Render
│   └── README.md
│
├── railway/           ← deploy de 1 clique na Railway
│   └── README.md
│
├── fly/               ← Fly.io (CLI `fly deploy`, ou o Launch da web UI)
│   ├── fly.toml
│   └── README.md
│
├── hf-spaces/         ← Hugging Face Spaces (Docker Space, nível de demo)
│   ├── Dockerfile
│   └── README.md
│
├── modal/             ← Modal (`modal deploy`, sempre no ar, Volume durável)
│   ├── modal_app.py
│   └── README.md
│
├── cloudflare/        ← Cloudflare Containers + D1 + R2 (serverless, escala a zero)
│   ├── Dockerfile        imagem do servidor + dialeto D1
│   ├── src/index.js      o Worker que fica na frente do container
│   ├── wrangler.jsonc
│   └── README.md
│
├── trycloudflare/     ← túnel rápido da Cloudflare (URL pública para um servidor LOCAL)
│   └── README.md
│
├── tailscale/         ← Tailscale (acesso privado do celular/tablet/notebook
│   └── README.md         pela tailnet; Funnel para o retorno de sandboxes na nuvem)
│
├── daytona/           ← guia do provedor de sandbox Daytona + o relay de saída
│   ├── wrangler.toml     em Cloudflare Worker para o nível gratuito dele; NÃO é
│   ├── src/index.js      um destino de deploy do servidor. Veja o README.md dele.
│   └── README.md
│
├── islo/              ← guia do provedor de sandbox Islo (injeção de credencial
│   └── README.md         pelo gateway); NÃO é um destino de deploy do servidor.
│
├── e2b/               ← guia do provedor de sandbox E2B (sobe a partir de um
│   └── README.md         template E2B pré-construído); NÃO é destino de deploy.
│
├── openshell/         ← guia do provedor de sandbox NVIDIA OpenShell (gateway
│   └── README.md         gRPC auto-hospedado, on-prem/isolado); NÃO é destino.
│
├── databricks/        ← Databricks Apps (Lakebase + UC Volumes)
│   ├── databricks.yml     configuração declarativa do bundle
│   ├── deploy.py          orquestrador de build + `bundle deploy`/`run`
│   ├── src/app.py         entrypoint do app (Lakebase + UC Volumes)
│   └── README.md
│
└── docker/            ← imagem Docker comum + stack do compose
    ├── Dockerfile         imagem slim multi-estágio (build web em node → builder python → runtime)
    ├── docker-compose.yaml   omnicraft + postgres para qualquer host Docker
    ├── entrypoint.py
    ├── .env.example
    ├── README.md
    └── SKILL.md
```

## Escolha o seu destino

| Se você quer … | Use | Onde olhar |
|---|---|---|
| **Publicar pelo navegador (sem ferramenta local)** | **Render ou Railway** | Botões acima: [Render](render/README.md) · [Railway](railway/README.md) |
| Experimentar o servidor no seu notebook | Docker compose | [`docker/README.md`](docker/README.md): `./bootstrap.sh` para gerar os segredos do `.env`, depois `docker compose up -d` |
| Rodar num host que você já tem (VPS, servidor de casa, on-prem) | Docker compose | [`docker/README.md`](docker/README.md): copie a stack do compose, `./bootstrap.sh`, depois `docker compose up -d` |
| Publicar na Fly.io | Fly | [`fly/README.md`](fly/README.md): `fly deploy`, SQLite num volume |
| Publicar na Modal (Volume durável para artefatos) | Modal | [`modal/README.md`](modal/README.md): `modal deploy`, com um Neon Postgres seu |
| Publicar serverless (escala a zero, sem VM/Postgres para administrar) | Cloudflare Containers + D1 + R2 | [`cloudflare/README.md`](cloudflare/README.md): `wrangler deploy` |
| Montar uma demo rápida (sem banco para provisionar) | HF Spaces | [`hf-spaces/README.md`](hf-spaces/README.md): Docker Space, SQLite |
| Compartilhar um servidor que roda no seu **notebook**: mostrar para colegas, ou deixar runners remotos e sandboxes na nuvem se conectarem de volta (sem publicar nada) | túnel rápido da Cloudflare | `cloudflared tunnel --url http://localhost:6767` |
| Acessar seu servidor de forma privada do **celular, tablet ou outros dispositivos pessoais** sem expô-lo à internet | Tailscale | [`tailscale/README.md`](tailscale/README.md): `tailscale serve https / http://localhost:8000` |
| Cloud Run / Kubernetes / outros | imagem Docker | [`docker/README.md`](docker/README.md), depois aponte sua plataforma para a imagem |
| Publicar num workspace Databricks (Lakebase + UC Volumes), autogerenciado | Databricks Apps | [`databricks/README.md`](databricks/README.md): usa Asset Bundles |

> **Está no Databricks?** O
> [OmniCraft no Databricks](https://docs.databricks.com/aws/en/omnicraft/)
> totalmente gerenciado (Beta) é o caminho recomendado: o Databricks opera o
> servidor por você, ligado à identidade do workspace, aos Foundation Models,
> ao AI Gateway e ao MLflow Tracing. Ative a prévia do **OmniCraft** nas
> configurações do seu workspace. O bundle autogerenciado de Databricks Apps
> acima é para quando você precisa de um controle que o serviço gerenciado
> ainda não expõe.

Todos os caminhos de deploy fora do Databricks compartilham a mesma imagem
(`docker/Dockerfile`): um container Python slim rodando o coordenador
FastAPI / WebSocket, com Postgres ou SQLite como armazenamento. O caminho do
Databricks Apps usa um entrypoint separado (`databricks/src/app.py`) que troca
o Postgres por Lakebase (PostgreSQL gerenciado) e o armazenamento de artefatos
por UC Volumes.

## Banco de dados: Postgres ou SQLite

O servidor suporta dois backends de banco, ambos de primeira classe (mesmo
schema, mesmas migrações; escolha pela `DATABASE_URL`):

- **Postgres**: o padrão e a resposta para produção. Obrigatório para mais de
  uma instância do servidor. **Gerenciado e provisionado automaticamente no
  deploy** na Render e na Railway. Em plataformas sem banco gerenciado (HF
  Spaces, Modal, ou Fly se você quiser Postgres em vez do SQLite em volume),
  traga o seu. O mais rápido é o **Neon**: crie um em [pg.new](https://pg.new)
  e defina a string de conexão como `DATABASE_URL`. Qualquer URL
  `postgres://` / `postgresql://` funciona (com pool ou direta); o entrypoint
  normaliza para o dialeto psycopg3 automaticamente.
- **SQLite**: um "nível leve" sem dependências, para demos e deploys de
  instância única, sem banco nenhum para provisionar. O arquivo `.db` fica no
  disco/volume persistente da plataforma (disco da Render, volume da Fly,
  volume da Railway) e sobrevive a reinícios ali; nos Spaces gratuitos da
  Hugging Face o disco é efêmero, então os dados do SQLite se perdem no
  restart, e na Modal a consistência eventual do Volume não combina com um
  arquivo `.db` vivo, então pule o nível SQLite por lá. Defina
  `DATABASE_URL=sqlite:////data/artifacts/chat.db`. O preço: só uma instância,
  e sem backups gerenciados.

**Quem provisiona o banco.** Render e Railway criam o Postgres *como parte do
deploy* (um passo só; ele pertence à sua conta na plataforma). Plataformas sem
banco gerenciado não fazem isso: lá você ou roda no SQLite (zero configuração,
efêmero na HF) ou traz um Postgres seu, como o Neon (um cadastro único, e
depois persistente). Um deploy não consegue provisionar sozinho um banco
*persistente* para você; persistência exige uma conta sua, e esse é o único
passo que não dá para automatizar.

**O primeiro boot contra um Postgres remoto é lento.** As migrações rodam pela
rede no primeiro boot (~1 minuto no Neon, contra quase instantâneo no SQLite
local); os boots seguintes são rápidos. Garanta que a tolerância do healthcheck
da plataforma aguente: Render e Railway aguentam por padrão; na Fly, aumente o
`grace_period` se você usar um banco remoto.

**Piso de memória:** o conjunto de trabalho do servidor é ~512 MB–1 GB. Render
Starter (512 MB), Railway (escala pelo uso) e HF Spaces passam disso
automaticamente; o padrão de 256 MB da Fly não passa, então a configuração da
Fly fixa uma máquina de 1 GB, e o app da Modal fixa `memory=1024` pelo mesmo
motivo.

## Modelo de execução

O OmniCraft roda em duas peças que conversam por um túnel WebSocket:

- **Servidor**: o app FastAPI que você publica aqui. Cuida das rotas
  HTTP / SSE, dos WebSockets de attach ao terminal, da persistência e da
  web UI.
- **Runner (host)**: um subprocesso Python que roda na **máquina do usuário**
  (notebook, dev container, etc.). Disca para o servidor via
  `WS /v1/runner/tunnel`, executa o loop do LLM + as ferramentas localmente, e
  devolve os eventos em streaming.

As opções de deploy aqui são todas sobre o servidor. Runners não são
publicados; cada usuário sobe o seu na própria máquina com
`omnicraft run …  --server <url>` ou `omnicraft claude  --server <url>`.

É essa separação que faz a imagem do servidor ser pequena (sem `tmux`, sem
SDKs de harness, sem chaves de API de LLM na imagem) e que faz nenhum código
de agente rodar dentro dela.

## Conecte seu notebook

Com o servidor no ar, entre pela sua máquina. O token é reaproveitado pelo
`run`, pelo `attach` e pelo `host`:

```bash
omnicraft login https://your-host
```

O `login` detecta o modo de autenticação do servidor automaticamente. Contas
embutidas, OIDC, proxies de header-auth e servidores hospedados no Databricks
(um Databricks App ou um caminho de API do workspace) funcionam todos com o
mesmo comando; no caso do Databricks ele roda o `databricks auth login` no
workspace certo para você (requer o extra `databricks`).

Depois registre a máquina como host, para que as sessões criadas na web UI
possam rodar nela:

```bash
omnicraft host https://your-host
```

Ou aponte uma execução avulsa direto para o servidor:

```bash
omnicraft run path/to/agent.yaml --server https://your-host
```

## Rode hosts em sandboxes na nuvem

Não quer que um notebook seja o host? Rode o host num sandbox na nuvem.

**Pela CLI (Modal, Daytona, Islo ou E2B).** Instale o extra do provedor quando
necessário (`pip install 'omnicraft[modal]'`, `'omnicraft[daytona]'` ou
`'omnicraft[e2b]'`; o Islo usa o cliente HTTP embutido), autentique
(`modal token new`, `DAYTONA_API_KEY`, `ISLO_API_KEY` ou `E2B_API_KEY`), e
então:

```bash
omnicraft sandbox create --provider modal     # ou --provider daytona / islo / e2b
omnicraft sandbox connect --provider modal --sandbox-id <id> --server https://your-host
```

> [!NOTE]
> A Modal limita a vida do sandbox a 24 horas. Rode `create` + `connect` de
> novo para levar o host a um sandbox novo. Daytona e Islo não têm limite de
> vida imposto pelo OmniCraft; organizações no nível gratuito da Daytona
> restringem a saída a uma allowlist; veja
> [`daytona/README.md`](daytona/README.md) para a solução com relay. O E2B
> compartilha o limite de 24 horas da Modal **e** sobe a partir de um
> *template* E2B pré-construído em vez de uma imagem de registry — construa o
> template uma vez antes; veja [`e2b/README.md`](e2b/README.md).

**Gerenciado pelo servidor (Modal, Daytona, Islo ou E2B).** Com os *hosts
gerenciados*, criar uma sessão com `"host_type": "managed"` (por exemplo,
`POST /v1/sessions {"agent_id": ..., "host_type": "managed"}`) faz o servidor
provisionar um sandbox, subir um host nele e rodar a sessão ali. Sem notebook,
sem passos de CLI por sessão; o sandbox é encerrado quando a sessão é apagada.
A configuração é uma seção `sandbox:` na configuração do servidor
(`omnicraft server -c config.yaml`, ou `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: modal
  server_url: https://your-host        # URL pública para onde os sandboxes discam de volta
```

As credenciais da Modal vêm do ambiente do servidor (`MODAL_TOKEN_ID` /
`MODAL_TOKEN_SECRET`, ou um `~/.modal.toml` montado), não do arquivo de
configuração. A Daytona lê `DAYTONA_API_KEY`; o Islo lê `ISLO_API_KEY` (e o
opcional `ISLO_BASE_URL`); o E2B lê `E2B_API_KEY` do ambiente do servidor. Cada
sandbox se autentica de volta com um token gerado pelo servidor, por
lançamento, então nenhuma credencial de usuário entra no sandbox.

**A imagem do host.** Os sandboxes sobem a partir da imagem oficial pré-pronta
do host (`ghcr.io/omnicraft-ai/omnicraft-host:latest`, publicada pela CI a
partir do alvo `host` do [`docker/Dockerfile`](docker/Dockerfile)), então o
host começa em segundos em vez de instalar o OmniCraft no boot. A imagem já traz
as CLIs dos harnesses de código (`claude`, `codex`, `pi`, `kiro-cli`), então
agentes de qualquer harness rodam no sandbox sem nada extra para instalar. Para
rodar sandboxes a partir da sua própria imagem (um fork, ou ferramentas extras
embutidas), construa o mesmo alvo `host` e aponte a configuração para ela:

```bash
docker build -f docker/Dockerfile --target host \
  -t docker.io/<you>/omnicraft-host:latest .
docker push docker.io/<you>/omnicraft-host:latest
```

```yaml
sandbox:
  provider: modal
  server_url: https://your-host
  modal:
    image: docker.io/<you>/omnicraft-host:latest
```

Para registries privados, defina `OMNICRAFT_MODAL_REGISTRY_SECRET` no servidor
com o nome de um secret da Modal que guarde `REGISTRY_USERNAME` /
`REGISTRY_PASSWORD`; para sandboxes lançados pela CLI,
`OMNICRAFT_MODAL_HOST_IMAGE` (ou `OMNICRAFT_DAYTONA_HOST_IMAGE` /
`OMNICRAFT_ISLO_HOST_IMAGE`) sobrescreve a referência da imagem.

**Credenciais de LLM para sessões gerenciadas.** Um sandbox novo não tem chave
de API nenhuma. Guarde as credenciais do seu provedor num
[secret da Modal](https://modal.com/secrets) e liste-o na configuração. As
variáveis de ambiente dele são injetadas em todo sandbox gerenciado, e o host
dentro do sandbox repassa as variáveis padrão de credencial dos harnesses
(`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
`CLAUDE_CODE_OAUTH_TOKEN`, `CODEX_ACCESS_TOKEN`, `OPENAI_API_KEY`,
`OPENAI_BASE_URL`, `GEMINI_API_KEY`, além dos apelidos com prefixo
`OMNICRAFT_`) para os runners dele:

```bash
modal secret create omnicraft-llm \
  OMNICRAFT_ANTHROPIC_API_KEY=sk-ant-… OPENAI_API_KEY=sk-…
```

Prefira `OMNICRAFT_ANTHROPIC_API_KEY` para autenticação do Claude Code por
chave de API. O OmniCraft a resolve no `apiKeyHelper` do Claude Code, evitando
uma `ANTHROPIC_API_KEY` crua no processo da CLI do Claude.

```yaml
sandbox:
  provider: modal
  server_url: https://your-host
  modal:
    secrets: [omnicraft-llm]
```

Para Daytona e Islo, liste os nomes das variáveis de ambiente do servidor em
`sandbox.daytona.env` ou `sandbox.islo.env`; o lançador copia os valores atuais
do ambiente do servidor para cada sandbox:

```yaml
sandbox:
  provider: islo
  server_url: https://your-host
  islo:
    env: [OPENAI_API_KEY, GIT_TOKEN]
```

Usa uma **assinatura do Claude** em vez de uma chave de API? Rode
`claude setup-token` na sua máquina e guarde o token de vida longa resultante
como `CLAUDE_CODE_OAUTH_TOKEN` no secret. Um **plano ChatGPT
Business/Enterprise** funciona igual, via um
[token de acesso do Codex](https://developers.openai.com/codex/enterprise/access-tokens)
guardado como `CODEX_ACCESS_TOKEN`. Para configurações de gateway ou outras
variáveis além do conjunto padrão, adicione
`OMNICRAFT_RUNNER_ENV_PASSTHROUGH=NAME1,NAME2` ao secret para nomear as
variáveis extras que o host deve repassar aos runners.

**Repositórios privados.** Sessões gerenciadas podem clonar um repositório como
workspace da sessão; para os privados, guarde um token HTTPS como `GIT_TOKEN`
num secret da Modal (GitLab: adicione `GIT_USERNAME=oauth2`). O helper de
credencial do git na imagem do host o usa para o clone e para os fetch/push
posteriores do agente.

O guia completo da Modal (sandboxes pela CLI, imagens próprias, credenciais de
LLM e do git, resolução de problemas) está em [`modal/README.md`](modal/README.md);
o guia da Daytona está em [`daytona/README.md`](daytona/README.md); o guia do
Islo (incluindo o modelo de injeção de credencial pelo gateway dele) está em
[`islo/README.md`](islo/README.md).

## Autenticação

A autenticação é comandada por uma única chave, `OMNICRAFT_AUTH_ENABLED`. O
padrão do framework (um `omnicraft server` local e puro) deixa desligado: modo
`header` de usuário único, sem login. Os deploys em container aqui
(Docker / HF / Render / Railway / Modal / Fly) definem
`OMNICRAFT_AUTH_ENABLED=1` por padrão nos entrypoints deles, já que uma
instância exposta na rede deve ser autenticada. Com a chave ligada, o modo é
escolhido pela sua configuração: forneça as variáveis `OMNICRAFT_OIDC_*` e você
tem `oidc`; caso contrário, tem o fluxo embutido `accounts`. O
`OMNICRAFT_AUTH_PROVIDER` é uma saída de emergência explícita, que fixa o modo
e sobrescreve essa escolha automática.

| Modo | Quando usar | O que é preciso |
|---|---|---|
| `accounts` (padrão do deploy) | Deploy autônomo, sem IdP externo: usuário/senha embutidos, com o primeiro usuário virando admin e convites pela UI. Ative com `OMNICRAFT_AUTH_ENABLED=1` (e sem variáveis de OIDC). | Defina `OMNICRAFT_ACCOUNTS_COOKIE_SECRET` (ou deixe o `bootstrap.sh` gerar) e `OMNICRAFT_ACCOUNTS_BASE_URL` (a URL pública). No primeiro boot, defina a senha do admin pelo formulário Create-admin na web, pelo prompt do terminal, ou por `--admin-password` / `OMNICRAFT_ACCOUNTS_INIT_ADMIN_PASSWORD`. |
| `oidc` | Deploy autônomo com o seu próprio IdP: o servidor cuida do fluxo de login inteiro | Defina `OMNICRAFT_AUTH_ENABLED=1` e as variáveis `OMNICRAFT_OIDC_*`; a presença de `OMNICRAFT_OIDC_ISSUER` seleciona o OIDC (ou fixe `OMNICRAFT_AUTH_PROVIDER=oidc`). Requer HTTPS (o cookie de sessão usa o prefixo `__Host-`). |
| `header` | Atrás de um proxy SSO já existente (oauth2-proxy, AWS ALB OIDC, Cloudflare Access, Tailscale Funnel, …) que injeta um header de identidade | O padrão quando `OMNICRAFT_AUTH_ENABLED` está desligado; ou fixe `OMNICRAFT_AUTH_PROVIDER=header`. Lê `X-Forwarded-Email` por padrão; defina `OMNICRAFT_AUTH_HEADER` para proxies que usam outro nome (ex.: `Cf-Access-Authenticated-User-Email`), e `OMNICRAFT_AUTH_HEADER_STRIP_PREFIX=accounts.google.com:` para o Google IAP. O proxy PRECISA remover qualquer cópia do header vinda do cliente. Headers ausentes são sempre rejeitados. |

> [!NOTE]
> **Sandboxes gerenciados precisam de `header`/`oidc` ou autenticação de usuário
> único.** O runner de cada sessão disca de volta com a identidade *do usuário*,
> que o modo embutido `accounts` (o padrão de deploy acima) não consegue
> fornecer pelo WebSocket do runner — ele devolve `403` mesmo com o host
> conectando. É a nível de framework; vale para todo provedor de sandbox
> (Modal / Daytona / Islo / Kubernetes / …).

### Login único (OIDC)

O fluxo embutido `accounts` não precisa de configuração além do próprio deploy.
Para deixar seu time entrar com as contas que já tem (Google, GitHub, Okta,
Microsoft), aponte o servidor para o seu provedor de identidade. No
`docker/.env` (ou nas configurações de ambiente da sua plataforma):

```dotenv
# A autenticação já está ligada (OMNICRAFT_AUTH_ENABLED=1) por padrão nos deploys aqui.
# Adicionar um issuer de OIDC vira a chave para login único. Sem flag extra.
OMNICRAFT_OIDC_ISSUER=https://accounts.google.com     # ou https://github.com / a URL do seu Okta / Entra
OMNICRAFT_DOMAIN=agents.yourcompany.com               # o domínio do seu servidor
OMNICRAFT_OIDC_CLIENT_ID=…
OMNICRAFT_OIDC_CLIENT_SECRET=…
```

```bash
docker compose up -d        # reinicie para aplicar
```

Seu time entra com as contas que já tem, e não sobra senha nenhuma para você
administrar. Nada mais no app muda.

> [!TIP]
> O único passo de fora é criar um app no seu provedor (por exemplo, no Google
> Cloud Console, ou em GitHub → Settings → Developer settings) para obter o
> client ID e o secret. Defina a **URL de callback** dele como
> `https://<your-domain>/auth/callback`.

**Decida quem pode entrar**, na configuração do seu servidor
(`/data/config.yaml`):

```yaml
allowed_domains: [yourcompany.com]    # só emails da sua empresa podem entrar
admins: [you@yourcompany.com]         # quem pode administrar os membros
```

> [!TIP]
> Precisa deixar entrar alguém de fora, digamos um terceirizado numa conta
> pessoal? Defina `OMNICRAFT_OIDC_ALLOW_INVITES=1` e mande um link de convite de
> uso único, em vez de abrir a allowlist inteira.

**Já tem um time nas contas embutidas?** Um comando leva todo mundo junto na
troca, para que mantenham as sessões e os direitos de admin:

```bash
omnicraft debug migrate-accounts-to-oidc <database-url> --domain yourcompany.com
```

Para os passo a passo específicos de cada provedor (GitHub OAuth, Google
Workspace, OIDC genérico), veja
[`docker/README.md#multi-user-mode-oidc`](docker/README.md#multi-user-mode-oidc).

### Modo header (X-Forwarded-Email)

> [!WARNING]
> Não publique um servidor compartilhado no modo header-auth a menos que você
> opere um proxy reverso confiável.

O modo `header` (`OMNICRAFT_AUTH_PROVIDER=header`) tira a identidade de quem
chama de um header confiável da requisição — `X-Forwarded-Email` por padrão.
Ele existe para deploys que ficam atrás de um proxy SSO (oauth2-proxy,
Cloudflare Access, um listener ALB/OIDC, Databricks Apps) que autentica o
usuário e injeta esse header em toda requisição.

Proxies que autenticam com outro nome de header definem
`OMNICRAFT_AUTH_HEADER` com esse nome, em vez de montar mais um salto só para
renomeá-lo. Por exemplo, atrás do **Cloudflare Access** (que fornece o email
autenticado em `Cf-Access-Authenticated-User-Email`):

```dotenv
OMNICRAFT_AUTH_PROVIDER=header
OMNICRAFT_AUTH_HEADER=Cf-Access-Authenticated-User-Email
```

Alguns proxies colocam um prefixo na identidade que injetam. O **Google IAP**
encaminha o email em `X-Goog-Authenticated-User-Email` prefixado com
`accounts.google.com:` (ex.: `accounts.google.com:user@example.com`). Defina
`OMNICRAFT_AUTH_HEADER_STRIP_PREFIX` para remover esse prefixo e recuperar o
email puro:

```dotenv
OMNICRAFT_AUTH_PROVIDER=header
OMNICRAFT_AUTH_HEADER=X-Goog-Authenticated-User-Email
OMNICRAFT_AUTH_HEADER_STRIP_PREFIX=accounts.google.com:
```

No modo header, **o servidor confia no que aquele header disser**. Se nenhum
proxy o define, as requisições são rejeitadas (`401`) em vez de silenciosamente
compartilharem uma identidade só. Mas um proxy *mal configurado* continua sendo
perigoso: se ele não **remover** qualquer cópia do header de identidade vinda do
cliente antes de encaminhar, qualquer pessoa pode se passar por qualquer outra
mandando o header ela mesma. Errar isso expõe as sessões, o histórico de
conversas, a saída de ferramentas e os arquivos de todo usuário a qualquer
outro chamador.

**Para quase todo mundo, use as contas embutidas (`accounts`, o padrão nestes
deploys) ou `oidc`**; os dois autenticam usuários no servidor, sem proxy nenhum
para acertar. Só escolha `header` quando você já opera um proxy em que confia
para definir e higienizar o header de identidade, e leia
[`docker/README.md#header-proxy-mode-for-deploys-behind-an-existing-sso-proxy`](docker/README.md#header-proxy-mode-for-deploys-behind-an-existing-sso-proxy)
antes.

## Adicionando um novo destino de deploy

Crie um subdiretório novo em `deploy/<target>/` com um `README.md` e um
`SKILL.md`. Se o novo destino usa a imagem Docker existente, o seu trabalho é
quase todo cola específica da plataforma (um `fly.toml`, um service.yaml do
Cloud Run, um Helm chart, uma configuração de HF Spaces) mais um README que
explique como apontar aquela plataforma para o `docker/Dockerfile`.

Atualize este README de topo com uma linha na tabela acima.
