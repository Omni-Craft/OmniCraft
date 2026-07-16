# OmniCraft na Modal

A [Modal](https://modal.com) tem dois papéis distintos para o OmniCraft:

1. **[Alvo de deploy do servidor](#publicando-o-servidor)** — rode o próprio
   servidor OmniCraft na Modal como um único servidor web sempre no ar
   (`modal_app.py` neste diretório).
2. **[Provedor de sandbox](#sandboxes-para-hosts-de-runner)** — máquinas
   descartáveis na nuvem para rodar *hosts* do OmniCraft, para que as sessões
   rodem na nuvem em vez de no seu notebook.

Os dois são independentes: você pode fazer o deploy do servidor em qualquer
lugar e ainda usar sandboxes da Modal para hosts, ou vice-versa.

## Publicando o servidor

Rode o servidor OmniCraft na Modal como um único servidor web sempre no ar.
O `modal_app.py` puxa a imagem padrão do servidor e lança o mesmo entrypoint
Docker que toda outra plataforma usa; a Modal fornece a URL HTTPS, o streaming
de logs, e um Volume persistente para o armazenamento de artefatos — bundles de agente
enviados sobrevivem a reinícios e redeploys aqui, diferente do Heroku ou da
Cloudflare.

### Pré-requisitos

- Uma conta na Modal e a CLI: `pip install modal && modal setup`. Não precisa
  de Docker local — os builders da Modal puxam a imagem.
- Um banco de dados Postgres. A Modal não tem Postgres gerenciado — o mais
  rápido é o **Neon**: crie um em [pg.new](https://pg.new) e copie a string
  de conexão.

### Deploy

```bash
# 1. Um secret bundle com os três valores obrigatórios. A URL do app é
#    determinística: https://<workspace>--omnicraft-server.modal.run
#    (o nome do seu workspace aparece em `modal profile current`).
modal secret create omnicraft-deploy \
  DATABASE_URL='postgres://…neon.tech/…' \
  OMNICRAFT_ACCOUNTS_COOKIE_SECRET="$(openssl rand -hex 32)" \
  OMNICRAFT_ACCOUNTS_BASE_URL='https://<workspace>--omnicraft-server.modal.run'

# 2. Publique.
modal deploy deploy/modal/modal_app.py
```

O `modal deploy` imprime a URL ao vivo — se ela for diferente do que você
chutou no passo 1 (ex.: um ambiente Modal não padrão adiciona um sufixo),
atualize o secret e faça o deploy de novo.

O primeiro boot roda as migrações do banco de dados pela rede (~1 minuto no
Neon).

**Pegue a senha do admin:** o primeiro boot a imprime no log do app:

```bash
modal app logs omnicraft
```

```
✓ Created initial admin account (accounts auth provider).
    password: <generated>
```

Entre como admin e convide seus colegas em **Members** na web UI.

> Para definir uma senha de admin conhecida em vez disso, adicione
> `OMNICRAFT_ACCOUNTS_INIT_ADMIN_PASSWORD=<password>` ao secret
> `omnicraft-deploy` antes do primeiro deploy.

### Ressalvas específicas da Modal

- **Limite de 2 MiB por mensagem de WebSocket.** O ingress da Modal limita as
  mensagens de WebSocket a 2 MiB cada, bem abaixo dos 100 MiB permitidos pelo
  próprio túnel do runner. O tráfego normal de streaming (eventos, frames de
  terminal) é bem menor, mas um payload de ferramenta muito grande sobre o
  túnel pode falhar nesta plataforma.
- **Conexões são reiniciadas no timeout de input de 24 h.** Um WebSocket
  intermediado ocupa um input de função da Modal, e os inputs têm um teto de
  24 horas — então um túnel vive no máximo um dia antes de ser cortado. Os
  runners reconectam automaticamente (backoff com jitter de 0,5–10 s).
- **Um único container sempre no ar, por design.** `min_containers=1` /
  `max_containers=1` em `modal_app.py`: o registro de runners fica em
  memória, então o tráfego precisa cair num único container, e o
  scale-to-zero mataria os túneis vivos. Não aumente `max_containers`
  esperando escalonamento horizontal.
- **Sem nível SQLite.** O Volume de artefatos é durável, mas não é lugar para
  um banco SQLite (semântica de consistência eventual); use Postgres.

### Use seu próprio IdP em vez disso (OIDC)

Adicione os valores do OIDC ao secret `omnicraft-deploy` (secrets da Modal são
bundles chave-valor; `modal secret create` com o mesmo nome o substitui) e
faça o deploy de novo:

```bash
modal secret create omnicraft-deploy \
  DATABASE_URL='…' \
  OMNICRAFT_AUTH_PROVIDER=oidc \
  OMNICRAFT_OIDC_ISSUER='https://github.com' \
  OMNICRAFT_OIDC_CLIENT_ID='…' \
  OMNICRAFT_OIDC_CLIENT_SECRET='…' \
  OMNICRAFT_OIDC_REDIRECT_URI='https://<workspace>--omnicraft-server.modal.run/auth/callback' \
  OMNICRAFT_OIDC_COOKIE_SECRET="$(openssl rand -hex 32)"
```

Os passos de registro no IdP (URLs de callback do GitHub / Google / Okta,
allow-list de domínio) são idênticos aos das outras plataformas — veja
[`deploy/render/README.md`](../render/README.md#use-o-seu-próprio-idp-oidc).

### Domínio personalizado

Passe `custom_domains=["omnicraft.example.com"]` para `@modal.web_server` em
`modal_app.py` (exige um plano pago da Modal), aponte seu DNS para a Modal
seguindo as instruções impressas, e atualize `OMNICRAFT_ACCOUNTS_BASE_URL`
(ou o redirect URI do OIDC) para combinar.

### Atualizando

`modal deploy deploy/modal/modal_app.py` de novo — a Modal reresolve
`ghcr.io/omnicraft-ai/omnicraft-server:latest`, então um redeploy é uma
atualização. O rollout substitui o container; os runners reconectam.

### Custo

A Modal cobra pelo uso real: memória a ~$0,008/GiB-hora e CPU pelo ciclo
(então a linha de CPU de um servidor ocioso é pequena). Uma instância de 1 GiB
sempre no ar custa por volta de **$6–8/mês**, o que cabe dentro dos
**$30/mês de créditos grátis** do plano Starter — tornando isso efetivamente
gratuito para um servidor com pouca carga. Preços:
[modal.com/pricing](https://modal.com/pricing).

## Sandboxes para hosts de runner

Sandboxes da Modal te dão máquinas descartáveis na nuvem para rodar hosts do
OmniCraft — sem notebook amarrado a uma sessão, sem VM para cuidar. Tem duas
formas de usá-los:

1. **Sandboxes lançados pela CLI** — você provisiona um sandbox pelo seu
   terminal e o registra como host no seu servidor. Bom para desenvolvimento
   e para rodar o código do seu checkout local na nuvem.
2. **Sandboxes gerenciados pelo servidor** — o servidor provisiona um sandbox
   automaticamente quando uma sessão é criada com `"host_type": "managed"`, e
   o encerra quando a sessão é apagada. Bom para deploys de produção, onde os
   usuários não deveriam precisar pensar em hosts.

Os dois inicializam a partir da imagem oficial pré-pronta do host, então o
boot leva segundos, não minutos.

### Pré-requisitos de sandbox

```bash
pip install 'omnicraft[modal]'   # installs the modal SDK extra
modal token new                  # one-time browser auth with Modal
```

O `modal token new` grava `~/.modal.toml`. Em qualquer lugar em que o
OmniCraft precise falar com a Modal (seu notebook para o fluxo da CLI, o
servidor para o fluxo gerenciado), as credenciais da Modal precisam estar
disponíveis — seja aquele arquivo, seja as variáveis de ambiente
`MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`.

### A imagem do host

Os sandboxes inicializam a partir de
`ghcr.io/omnicraft-ai/omnicraft-host:latest`, uma imagem publicada pela CI a
partir do alvo `host` do [`deploy/docker/Dockerfile`](../docker/Dockerfile),
com o OmniCraft e suas dependências pré-instaladas — incluindo as CLIs dos
harnesses de código (`claude`, `codex`, `pi`, `kiro-cli`), então agentes de
qualquer harness rodam sem instalação dentro do sandbox.

Para usar uma imagem diferente (um fork, ou ferramentas extras embutidas),
construa o mesmo alvo e envie para onde a Modal conseguir puxar:

```bash
docker build -f deploy/docker/Dockerfile --target host \
  -t docker.io/<you>/omnicraft-host:latest .
docker push docker.io/<you>/omnicraft-host:latest
```

Depois aponte o OmniCraft para ela — `OMNICRAFT_MODAL_HOST_IMAGE` para o
fluxo da CLI, ou `sandbox.modal.image` na config do servidor para o fluxo
gerenciado (veja abaixo). Para registries privados, defina
`OMNICRAFT_MODAL_REGISTRY_SECRET` como o nome de um
[secret da Modal](https://modal.com/secrets) contendo `REGISTRY_USERNAME` /
`REGISTRY_PASSWORD`.

> [!NOTE]
> Construindo em Apple Silicon? Passe `--platform linux/amd64` — os sandboxes
> da Modal rodam x86_64.

### Sandboxes lançados pela CLI

Provisione um sandbox e envie o seu checkout local para dentro dele:

```bash
omnicraft sandbox create --provider modal
```

Isso puxa a imagem do host, constrói wheels a partir do seu checkout local, e
as sobrepõe — então o sandbox roda o *seu* código, não o que a imagem foi
construída a partir de. Depois registre-o como host no seu servidor:

```bash
omnicraft sandbox connect --provider modal \
  --sandbox-id <id-printed-by-create> \
  --server https://your-host
```

O `connect` roda `omnicraft host` dentro do sandbox e mantém a conexão aberta
no seu terminal — Ctrl-C a derruba. Sessões novas apontando para aquele host
agora rodam no sandbox.

Rodando vários sandboxes contra um servidor? Passe um `--host-name <label>`
único para cada `connect` — o servidor indexa hosts por (owner, name), e
sandboxes que compartilham um hostname colidem.

Sandboxes são descartáveis. Quando seu código muda, crie um novo.

> [!NOTE]
> A Modal limita a vida do sandbox a 24 horas (um limite rígido da
> plataforma). Rode `create` + `connect` de novo para levar o host a um
> sandbox novo.

Para o ciclo de vida do lado do provedor (listar / status / terminar), use as
próprias ferramentas da Modal — o
[dashboard da Modal](https://modal.com/sandboxes) ou a CLI `modal`.

### Conectando a um servidor autenticado

O `connect` roda `omnicraft host` dentro do sandbox, e esse host precisa
apresentar credenciais quando disca de volta para um servidor que exige
autenticação. O fluxo interativo do navegador do `omnicraft login` não
consegue rodar dentro de um sandbox, então injete as chaves do servidor
relevante: guarde-as num [secret da Modal](https://modal.com/secrets) e
nomeie-o em `OMNICRAFT_MODAL_SANDBOX_SECRETS` (separado por vírgulas) antes
de rodar `create`:

```bash
modal secret create omnicraft-server-auth \
  DATABRICKS_HOST=https://example.databricks.com \
  DATABRICKS_TOKEN=<your-pat>
export OMNICRAFT_MODAL_SANDBOX_SECRETS=omnicraft-server-auth
omnicraft sandbox create --provider modal
```

O host dentro do sandbox gera um bearer token novo a partir dessas
credenciais em cada connect e reconexão. Para um servidor atrás da
autenticação Databricks, injete `DATABRICKS_HOST` mais `DATABRICKS_TOKEN` (um
PAT) ou `DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` (um service
principal OAuth — a regeração mantém um sandbox de vida longa conectado
mesmo depois do vencimento de um token individual).

Um servidor sem autenticação no túnel do host não precisa de nada disso, e
nem os [sandboxes gerenciados pelo servidor](#sandboxes-gerenciados-pelo-servidor)
precisam — eles se autenticam com um token por lançamento gerado pelo
servidor, automaticamente.

(A mesma variável de ambiente também carrega credenciais de LLM / git para
sandboxes lançados pela CLI — qualquer secret nomeado em
`OMNICRAFT_MODAL_SANDBOX_SECRETS` cai no ambiente do sandbox, exatamente como
`sandbox.modal.secrets` faz para lançamentos gerenciados.)

### Sandboxes gerenciados pelo servidor

Com hosts gerenciados, o servidor faz tudo isso acima por sessão. Adicione
uma seção `sandbox:` na config do servidor (`omnicraft server -c
config.yaml`, ou `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: modal
  server_url: https://your-host    # public URL sandboxes dial back to
```

`server_url` precisa ser alcançável *a partir da nuvem da Modal* — uma URL
HTTPS pública, não `localhost`. O próprio servidor precisa de credenciais da
Modal no seu ambiente (`MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`, ou um
`~/.modal.toml` montado).

Agora crie sessões com `host_type: "managed"`:

```bash
curl -X POST https://your-host/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "agent_...", "host_type": "managed"}'
```

O create retorna imediatamente; o servidor provisiona um sandbox novo em
segundo plano, inicia um host nele, e vincula a sessão assim que o host fica
online (`host_id` / `workspace` aparecem em `GET /v1/sessions/{id}` quando
isso acontece). Uma mensagem enviada antes disso espera o lançamento se
estabilizar, então você pode mandar o primeiro prompt logo de cara. Apagar a
sessão termina o sandbox e remove o host. Cada sandbox se autentica de volta
com um token por lançamento gerado pelo servidor — nenhuma credencial de
usuário entra no sandbox.

Configurações `modal:` opcionais:

```yaml
sandbox:
  provider: modal
  server_url: https://your-host
  modal:
    image: docker.io/<you>/omnicraft-host:latest   # default: official image
    secrets: [omnicraft-llm]                       # Modal secrets to inject
```

### Credenciais de LLM para sandboxes gerenciados

Um sandbox novo não tem nenhuma chave de API. Guarde as credenciais do seu
provedor num [secret da Modal](https://modal.com/secrets) e liste-o em
`sandbox.modal.secrets` — as variáveis de ambiente dele são injetadas em todo
sandbox gerenciado, e o host dentro do sandbox repassa as variáveis padrão de
credencial do harness para os seus runners:

```bash
modal secret create omnicraft-llm \
  OMNICRAFT_ANTHROPIC_API_KEY=sk-ant-… OPENAI_API_KEY=sk-…
```

O conjunto repassado cobre as variáveis que os próprios harnesses resolvem —
e ele vai muito além das APIs de primeira parte. As variáveis `*_BASE_URL`
redirecionam cada harness para *qualquer* endpoint compatível, então o mesmo
mecanismo cobre provedores de fronteira, gateways como
[OpenRouter](https://openrouter.ai) e [LiteLLM](https://docs.litellm.ai), e
modelos open-source auto-hospedados:

| Variável | Habilita |
|---|---|
| `OMNICRAFT_ANTHROPIC_API_KEY` ou `ANTHROPIC_API_KEY` | Modelos Claude na API da Anthropic (harnesses claude-sdk, pi, claude-code). Prefira a forma `OMNICRAFT_` para o Claude Code, para que a `ANTHROPIC_API_KEY` crua não fique presente no processo da CLI. |
| `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` | Gateways compatíveis com Anthropic — aponte o claude-code para um proxy LiteLLM, uma ponte Bedrock/Vertex, ou um gateway corporativo |
| `CLAUDE_CODE_OAUTH_TOKEN` | claude-code com uma assinatura Claude (sem chave de API) |
| `OPENAI_API_KEY` | Modelos OpenAI na API da OpenAI (harnesses codex, openai-agents) |
| `OPENAI_BASE_URL` | Qualquer endpoint compatível com OpenAI — o padrão de-facto do ecossistema de modelos abertos. Gateways (OpenRouter, LiteLLM), provedores hospedados de pesos abertos (Together, Fireworks, Groq), ou vLLM / Ollama auto-hospedados — é assim que Llama, Qwen, DeepSeek e companhia se conectam |
| `CODEX_ACCESS_TOKEN` | codex com um workspace ChatGPT Business/Enterprise |
| `GEMINI_API_KEY` | Modelos Gemini na API do Google AI |

Configurações comuns:

- **Claude com uma chave de API** — coloque `OMNICRAFT_ANTHROPIC_API_KEY` no
  secret. O OmniCraft a resolve no `apiKeyHelper` do Claude Code; não defina
  também `ANTHROPIC_API_KEY`, a menos que você não se importe com o Claude
  Code detectando a variável de ambiente crua da chave customizada.
- **Claude com uma assinatura** — rode `claude setup-token` na sua própria
  máquina (autenticação única pelo navegador) e guarde o token de vida longa
  resultante como `CLAUDE_CODE_OAUTH_TOKEN`.
- **Codex com uma chave de API** — coloque `OPENAI_API_KEY` no secret.
- **Codex com um plano ChatGPT Business/Enterprise** — gere um
  [token de acesso do Codex](https://developers.openai.com/codex/enterprise/access-tokens)
  no console de admin do ChatGPT (um admin do workspace precisa conceder a
  permissão) e guarde-o como `CODEX_ACCESS_TOKEN`.
- **Codex com um plano ChatGPT Plus/Pro** — não existe token headless para
  planos pessoais. O Codex guarda a autenticação de plano pessoal em
  `~/.codex/auth.json` com refresh tokens efetivamente de uso único, então
  cópias desse arquivo entre máquinas invalidam umas às outras — não dá para
  injetar num sandbox descartável via um secret compartilhado. Use uma chave
  de API ou `codex login --device-auth` dentro de um sandbox de vida longa
  em vez disso (o login por device-code precisa primeiro ser ativado em
  ChatGPT → Settings → Security).
- **Gateways e modelos open-source** — defina `OPENAI_BASE_URL` para o
  endpoint mais sua chave como `OPENAI_API_KEY` (ex.:
  `OPENAI_BASE_URL=https://openrouter.ai/api/v1` com uma chave do
  OpenRouter, ou a URL do seu próprio servidor vLLM). Gateways do lado da
  Anthropic funcionam do mesmo jeito, via `ANTHROPIC_BASE_URL` +
  `ANTHROPIC_AUTH_TOKEN`.

Para variáveis de ambiente além do conjunto padrão, adicione
`OMNICRAFT_RUNNER_ENV_PASSTHROUGH=NAME1,NAME2` ao secret — o host repassa os
extras nomeados para os seus runners.

Para conferir o que de fato chegou num sandbox, dê exec nele com a CLI da
Modal e inspecione o ambiente:

```bash
modal shell <sandbox-id>          # interactive shell in the sandbox
env | grep -E 'ANTHROPIC|OPENAI|GIT'
```

### Credenciais do Git (repositórios privados)

Sandboxes clonam workspaces de repositório anonimamente por padrão, o que
cobre só repositórios públicos. Para repositórios privados — tanto o clone
que o servidor roda na criação da sessão quanto o `git fetch` / `git push`
que o agente roda depois — coloque um token HTTPS num secret da Modal como
`GIT_TOKEN`:

```bash
modal secret create omnicraft-git GIT_TOKEN=github_pat_…
```

e liste o secret em `sandbox.modal.secrets` (vários secrets se compõem, então
manter credenciais de git e de LLM em secrets separados funciona bem):

```yaml
sandbox:
  provider: modal
  server_url: https://your-host
  modal:
    secrets: [omnicraft-llm, omnicraft-git]
```

A imagem do host traz um helper de credencial git que responde pela
autenticação HTTPS a partir de `GIT_TOKEN`, então nada é escrito em disco e
nenhuma URL nunca embute o token. Detalhes por provedor:

- **GitHub** — use um [personal access token de granularidade
  fina](https://github.com/settings/personal-access-tokens) escopado aos
  repositórios que o sandbox precisa (Contents: read, ou read/write se o
  agente fizer push). O usuário de autenticação padrão (`x-access-token`) já
  está correto.
- **GitLab** — crie um token de projeto ou pessoal com `read_repository` /
  `write_repository` e adicione `GIT_USERNAME=oauth2` ao secret.
- **Outros remotes HTTPS** — qualquer servidor que aceite basic auth
  funciona; defina `GIT_USERNAME` se ele exigir um usuário específico.

Use URLs de repositório HTTPS (`https://github.com/org/repo`) para workspaces
privados — URLs SSH (`git@github.com:…`) precisariam de uma chave e de
configuração de known-hosts dentro do sandbox, o que o fluxo gerenciado não
fornece.

O token é repassado host→runner (como as credenciais de LLM acima), então os
próprios comandos git do agente se autenticam do mesmo jeito que o clone no
lançamento. Se o agente também deve criar commits, embuta ou configure
`user.name` / `user.email` pelas instruções do seu agente ou por uma imagem
customizada.

### Referência de variáveis de ambiente

| Variável | Onde é lida | Propósito |
|---|---|---|
| `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` | máquina da CLI / servidor | Credenciais da API da Modal (alternativa ao `~/.modal.toml`) |
| `OMNICRAFT_MODAL_HOST_IMAGE` | máquina da CLI / servidor | Sobrescreve a referência da imagem do host (`sandbox.modal.image` tem precedência para o gerenciado) |
| `OMNICRAFT_MODAL_REGISTRY_SECRET` | máquina da CLI / servidor | Nome do secret da Modal com `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` para pulls de imagem privada |
| `OMNICRAFT_MODAL_SANDBOX_SECRETS` | máquina da CLI / servidor | Nomes de secrets da Modal, separados por vírgula, para injetar (`sandbox.modal.secrets` tem precedência para o gerenciado) |
| `OMNICRAFT_RUNNER_ENV_PASSTHROUGH` | dentro do sandbox (definida via um secret da Modal) | Nomes de variáveis de ambiente extras que o host repassa aos runners |
| `GIT_TOKEN` | dentro do sandbox (definida via um secret da Modal) | Token HTTPS para clone / fetch / push de repositório privado |
| `GIT_USERNAME` | dentro do sandbox (definida via um secret da Modal) | Usuário de autenticação pareado com `GIT_TOKEN` (padrão `x-access-token`; o GitLab usa `oauth2`) |

Tudo acima é configuração pública suportada. As variáveis que o próprio
launcher gerenciado define dentro do sandbox —
`OMNICRAFT_HOST_TOKEN`, `OMNICRAFT_HOST_ID`, `OMNICRAFT_HOST_NAME` — são
encanamento interno (geradas pelo servidor por lançamento) e nunca são
definidas por usuários.

### Limites e resolução de problemas

- **Vida de 24 horas.** A Modal impõe um teto rígido de 24 horas na vida do
  sandbox. Fluxo da CLI: rode `create` + `connect` de novo. Fluxo gerenciado:
  nada a fazer — quando o sandbox morre, a próxima mensagem para a sessão
  provisiona um novo sob o mesmo host (o vínculo da sessão sobrevive; um
  workspace de repositório é reclonado). Mudanças não commitadas no workspace
  morrem com o sandbox, então dê push no trabalho que você se importa.
- **Recursos.** Sandboxes são criados com 2 CPUs e 4 GiB de memória.
- **Lançamento gerenciado trava e depois falha.** O servidor espera até dois
  minutos pelo host dentro do sandbox ficar online. Se der timeout, confira
  se `server_url` é alcançável publicamente a partir da Modal, depois
  inspecione o log do host dentro do sandbox: `/tmp/omnicraft-host.log`.
- **Falhas de pull de imagem.** Imagem privada sem
  `OMNICRAFT_MODAL_REGISTRY_SECRET` definida, ou um secret sem
  `REGISTRY_USERNAME` / `REGISTRY_PASSWORD`.
- **Agente sem credenciais.** Confira se o secret da Modal está listado em
  `sandbox.modal.secrets` e se os nomes das variáveis combinam com o conjunto
  repassado acima (ou estão nomeados em
  `OMNICRAFT_RUNNER_ENV_PASSTHROUGH`).
