# Provider CoreWeave Sandbox

O [CoreWeave Sandbox](https://docs.coreweave.com/products/sandboxes) te dá
máquinas de nuvem descartáveis para rodar hosts do OmniCraft, de duas formas:

- **Lançado pela CLI**: `omnicraft sandbox create` / `connect` provisiona um
  sandbox a partir do seu terminal, envia o seu checkout local para dentro
  dele, e o registra como host no seu servidor.
- **Gerenciado pelo servidor**: o servidor provisiona um sandbox
  automaticamente quando uma sessão é criada com `"host_type": "managed"` e o
  encerra quando a sessão é apagada.

O launcher encapsula o SDK Python oficial do
[`cwsandbox`](https://github.com/coreweave/cwsandbox-client), protegido pelo
extra `cwsandbox` e importado de forma preguiçosa — a mesma postura dos
launchers do Modal e do Daytona. Os sandboxes inicializam a partir da imagem
oficial pré-pronta do host, então o startup leva segundos.

Duas características moldam o resto deste guia:

- **Sem port forward local.** O CoreWeave Sandbox não consegue encaminhar uma
  porta de callback sandbox→notebook, então o passo interativo de
  `omnicraft login` / App OAuth dentro do sandbox é pulado automaticamente
  (como no Modal e no Daytona) — tudo bem para servidores autenticados por
  token/OIDC.
- **Sem egress por padrão.** O CW Sandbox bloqueia tráfego de saída a menos
  que seja pedido; o launcher solicita `egress_mode: internet` para que o
  host consiga alcançar o seu servidor e o agente consiga alcançar o endpoint
  do seu modelo.

```bash
pip install 'omnicraft[cwsandbox]'
```

## Pré-requisitos

Crie uma chave de API do CoreWeave Sandbox e deixe-a disponível onde o
launcher roda — o seu shell para o fluxo de CLI, o processo do **servidor**
para sandboxes gerenciados (12-factor; nunca em arquivos de configuração):

```bash
export CWSANDBOX_API_KEY=...                          # chave de API do CoreWeave Sandbox
export CWSANDBOX_BASE_URL=https://api.cwsandbox.com   # opcional (este é o padrão)
```

## A imagem do host

Os sandboxes inicializam a partir de
`ghcr.io/omnicraft-ai/omnicraft-host:latest`, publicada pela CI a partir do
alvo `host` do [`deploy/docker/Dockerfile`](../docker/Dockerfile) com o
OmniCraft e as suas dependências pré-instaladas — incluindo as CLIs dos
harnesses de código (`claude`, `codex`, `pi`, `kiro-cli`), então agentes de
qualquer harness rodam sem instalação dentro do sandbox.

Para usar uma imagem diferente (um fork, ou ferramentas extras embutidas),
construa o mesmo alvo e envie para qualquer lugar de onde o CoreWeave possa
puxar:

```bash
docker build -f deploy/docker/Dockerfile --target host \
  --platform linux/amd64 \
  -t docker.io/<you>/omnicraft-host:latest .
docker push docker.io/<you>/omnicraft-host:latest
```

Depois aponte o OmniCraft para ela — `OMNICRAFT_CWSANDBOX_HOST_IMAGE` para o
fluxo de CLI, ou `sandbox.cwsandbox.image` na configuração do servidor para o
fluxo gerenciado.

> [!NOTE]
> Construindo em Apple Silicon? Passe `--platform linux/amd64` — os sandboxes
> rodam em x86_64.

## Sandboxes lançados pela CLI

Provisione um sandbox e envie o seu checkout local para dentro dele:

```bash
omnicraft sandbox create --provider cwsandbox --server https://your-host
```

Isso puxa a imagem do host, constrói wheels a partir do seu checkout local, e
as sobrepõe — então o sandbox roda *o seu* código, não o que a imagem foi
construída a partir de. Depois registre-o como host no seu servidor:

```bash
omnicraft sandbox connect --provider cwsandbox \
  --sandbox-id <id-printed-by-create> \
  --server https://your-host
```

O `connect` roda o `omnicraft host` dentro do sandbox e mantém a conexão
aberta no seu terminal — Ctrl-C derruba tudo. Novas sessões apontando para
esse host agora rodam no sandbox.

Rodando vários sandboxes contra um servidor? Passe um `--host-name <label>`
único para cada `connect` — o servidor indexa hosts por (dono, nome), e
sandboxes que compartilham um nome colidem.

Sandboxes são descartáveis. Quando o seu código mudar, crie um novo.

Para injetar credenciais de LLM/git num sandbox lançado pela CLI, defina
`OMNICRAFT_CWSANDBOX_SANDBOX_ENV` no seu shell com uma lista de nomes de
variável separados por vírgula (ex.: `ANTHROPIC_API_KEY,GIT_TOKEN`) antes de
rodar `create` — as variáveis nomeadas são copiadas do seu ambiente para
dentro do sandbox no momento do provisionamento. Um nome listado que **não**
está definido faz o lançamento falhar ruidosamente (senão isso apareceria
bem mais tarde como uma falha opaca de autenticação do harness dentro do
sandbox).

### Conectando a um servidor autenticado

O `connect` roda o `omnicraft host` dentro do sandbox, e esse host precisa
apresentar credenciais quando disca de volta para um servidor que exige
autenticação. O fluxo interativo de navegador do `omnicraft login` não
consegue rodar dentro de um sandbox (sem encaminhamento de porta de
callback), então injete as chaves do servidor em questão — nomeie-as em
`OMNICRAFT_CWSANDBOX_SANDBOX_ENV` antes do `create`:

```bash
export OMNICRAFT_CWSANDBOX_SANDBOX_ENV=DATABRICKS_HOST,DATABRICKS_TOKEN
omnicraft sandbox create --provider cwsandbox --server https://your-host
```

O host dentro do sandbox gera um bearer token novo a partir dessas
credenciais a cada connect e reconnect. Para um servidor por trás do
Databricks, injete `DATABRICKS_HOST` mais `DATABRICKS_TOKEN` (um PAT) ou
`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` (um service principal
OAuth — regerar mantém um sandbox de vida longa conectado além da expiração
de um único token).

Um servidor sem autenticação no túnel do host não precisa de nada disso, e
[sandboxes gerenciados pelo servidor](#sandboxes-gerenciados-pelo-servidor)
também não — eles se autenticam com um token gerado pelo servidor por
lançamento, automaticamente.

## Sandboxes gerenciados pelo servidor

Adicione uma seção `sandbox:` na configuração do servidor (`omnicraft server
-c config.yaml`, ou `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: cwsandbox
  server_url: https://your-host    # URL pública para onde os sandboxes discam de volta
```

`provider` + `server_url` já é uma configuração completa. O `server_url`
**precisa ser alcançável a partir do CoreWeave** — o host dentro do sandbox
abre um WebSocket de saída para ele, não para `localhost`. Para testes
locais, exponha o seu servidor com um túnel (`cloudflared` / `ngrok`) e
aponte `server_url` para a URL do túnel. O próprio servidor precisa de
`CWSANDBOX_API_KEY` (e opcionalmente `CWSANDBOX_BASE_URL`) no seu ambiente.

Sessões criadas com `host_type: "managed"` (a chamada de API ou a opção New
Sandbox da Web UI) rodam então num CW sandbox novo; o create retorna
imediatamente e o provisionamento acontece em segundo plano, exatamente como
o [fluxo gerenciado do Modal](../modal/README.md#sandboxes-gerenciados-pelo-servidor) —
incluindo workspaces de repositório, o rendezvous da primeira mensagem, e o
relançamento de sandbox morto.

```bash
curl -X POST https://your-host/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "agent_...", "host_type": "managed"}'
```

Cada sandbox gerenciado se autentica de volta com um token gerado pelo
servidor, por lançamento; nenhuma credencial de usuário entra no sandbox para
a conexão com o servidor.

Configurações `cwsandbox:` opcionais:

```yaml
sandbox:
  provider: cwsandbox
  server_url: https://your-host
  cwsandbox:
    image: docker.io/<you>/omnicraft-host:latest        # padrão: a imagem oficial
    env: [OPENAI_API_KEY, ANTHROPIC_API_KEY, GIT_TOKEN]  # NOMES de variável de ambiente do servidor a injetar
```

### Hosts gerenciados e autenticação do servidor

Como a discagem de volta se autentica depende de como o **servidor** faz a
autenticação, e há uma interação que vale a pena conhecer antes de fazer o
deploy. Um sandbox gerenciado abre dois tipos de conexão de volta para o
servidor: o **túnel do host**, que o token por lançamento autentica
diretamente (sempre funciona), e um **túnel de runner** por sessão, aberto
pelo subprocesso do runner — que se autentica com qualquer credencial de
servidor que consiga resolver, **não** o token de host por lançamento.

A consequência:

- **Autenticação por header / proxy OIDC, ou servidores single-user
  (sem autenticação)** — o túnel de runner não precisa de identidade extra
  nenhuma, então hosts gerenciados funcionam prontos para uso.
- **O provider embutido `accounts` (`OMNICRAFT_AUTH_ENABLED=1`)** — o túnel
  de runner exige, além disso, uma identidade de *usuário*, que o token de
  host por lançamento não carrega, então a discagem de volta do runner é
  recusada (`403`) mesmo com o túnel do host conectando. Essa é uma interação
  de hosts gerenciados a nível de framework, compartilhada por **todos** os
  providers de sandbox (Modal / Daytona / Islo / cwsandbox), não específica
  do cwsandbox.

Então, para um deploy de cwsandbox gerenciado, coloque o servidor atrás de
**autenticação por header ou OIDC** (um proxy reverso / IdP injeta a
identidade do usuário em toda requisição, incluindo o WebSocket de runner —
veja [`deploy/README.md#autenticação`](../README.md#autenticação)), ou rode-o
single-user. O provider `accounts` funciona bem para hosts lançados pela CLI
(você faz `omnicraft login`, e esse token é o que o host dentro do sandbox
repassa), mas ainda não para a discagem de volta do runner gerenciado.

## Credenciais de modelo (chaves de LLM)

Um sandbox novo não tem credencial de modelo nenhuma. Nomeie as variáveis a
injetar em `OMNICRAFT_CWSANDBOX_SANDBOX_ENV` (CLI) ou `sandbox.cwsandbox.env`
(gerenciado); o launcher copia o valor do ambiente de lançamento para dentro
do sandbox, e o host dentro do sandbox repassa as variáveis padrão de
credencial do harness para os seus runners:

```bash
export ANTHROPIC_API_KEY=sk-ant-…   # no servidor (gerenciado) ou no seu shell (CLI)
```

```yaml
sandbox:
  provider: cwsandbox
  server_url: https://your-host
  cwsandbox:
    env: [ANTHROPIC_API_KEY]
```

Quais variáveis injetar — providers, gateways, assinaturas, git — é idêntico
ao Modal; veja a [tabela de variáveis e as receitas por
plano](../modal/README.md#credenciais-de-llm-para-sandboxes-gerenciados) e as
[credenciais de git](../modal/README.md#credenciais-do-git-repositórios-privados).
Para uma **assinatura** do Claude especificamente, rode `claude setup-token`
na sua própria máquina (autenticação de navegador única) e injete o token de
vida longa resultante como `CLAUDE_CODE_OAUTH_TOKEN`. Para variáveis de
ambiente além do conjunto padrão, injete
`OMNICRAFT_RUNNER_ENV_PASSTHROUGH=NAME1,NAME2`.

## Credenciais de git (repositórios privados)

Injete um token HTTPS como `GIT_TOKEN` (GitLab: adicione
`GIT_USERNAME=oauth2`) via `OMNICRAFT_CWSANDBOX_SANDBOX_ENV` /
`sandbox.cwsandbox.env`. O credential helper de git da imagem do host
responde à autenticação HTTPS a partir dele tanto para o clone no momento do
lançamento quanto para o `fetch` / `push` posterior do agente, sem escrever
nada em disco. Use URLs de repositório HTTPS. Detalhes por provider batem com
o [guia de git do Modal](../modal/README.md#credenciais-do-git-repositórios-privados).

## Considerações de segurança

- **Credenciais injetadas vivem no control plane do CoreWeave.** O launcher
  passa os valores de `sandbox.cwsandbox.env` para a API do CoreWeave como
  variáveis de ambiente do sandbox, então um terceiro detém tudo que você
  injetar (chaves de LLM, `GIT_TOKEN`) pelo tempo de vida do sandbox. Prefira
  credenciais **com escopo restrito e de vida curta**: um PAT granular
  limitado aos repositórios que uma sessão precisa, um token de gateway em
  vez de uma chave raiz do provider.
- **Todos os sandboxes gerenciados compartilham uma org do CoreWeave + uma
  `CWSANDBOX_API_KEY`.** O isolamento entre usuários se apoia inteiramente
  nas fronteiras de sandbox do CoreWeave, e a chave compartilhada consegue
  enumerar e apagar qualquer sandbox — a mesma forma de org de tenant único
  dos providers Modal e Daytona. Restrinja a org só a essa carga de trabalho.
- **O tempo de vida do token de lançamento acompanha o tempo de vida do
  sandbox.** O tempo de vida do CW Sandbox é sobrescrevível pelo operador
  (`OMNICRAFT_CWSANDBOX_MAX_LIFETIME_S`, padrão de 24h), então o TTL do token
  de host por lançamento é derivado dele — sempre acima do teto, para que um
  sandbox vivo consiga se reautenticar entre reconexões enquanto um token
  vazado não consegue durar mais que o sandbox de onde veio. Um relançamento
  gera um novo.

## Notas / limites

- Sandboxes são recolhidos em `max_lifetime_seconds` (padrão de 24h;
  sobrescreva com `OMNICRAFT_CWSANDBOX_MAX_LIFETIME_S`). O TTL do token de
  lançamento gerenciado é definido acima disso para que as reconexões
  continuem funcionando.
- Egress assume nenhum por padrão no CW Sandbox; o launcher solicita
  `egress_mode: internet` para que o host consiga alcançar o servidor e o
  agente consiga alcançar o endpoint do seu modelo.

## Troubleshooting

- **"managed host did not come online within 120s"** — o servidor espera até
  dois minutos pelo host dentro do sandbox se registrar. Se der timeout,
  confira se `server_url` está publicamente alcançável a partir do CoreWeave,
  depois inspecione o log do host dentro do sandbox:
  `/tmp/omnicraft-host.log`.
- **Primeiro lançamento lento** — o primeiro lançamento a partir de uma
  imagem espera um pull frio do registry antes do sandbox ficar pronto;
  lançamentos seguintes reaproveitam a imagem em cache e começam em segundos.
- **Agente sem credenciais** — verifique se os nomes das variáveis injetadas
  batem com o conjunto repassado (ou estão nomeados em
  `OMNICRAFT_RUNNER_ENV_PASSTHROUGH`), e se cada nome foi de fato definido no
  ambiente de lançamento.

## Referência de variáveis de ambiente

| Variável | Onde é lida | Finalidade |
|---|---|---|
| `CWSANDBOX_API_KEY` | máquina da CLI / servidor | Credenciais de API do CoreWeave Sandbox (obrigatório) |
| `CWSANDBOX_BASE_URL` | máquina da CLI / servidor | Endpoint de API não padrão do CW Sandbox (padrão `https://api.cwsandbox.com`) |
| `OMNICRAFT_CWSANDBOX_HOST_IMAGE` | máquina da CLI / servidor | Sobrescreve a referência da imagem do host (`sandbox.cwsandbox.image` tem precedência no modo gerenciado) |
| `OMNICRAFT_CWSANDBOX_SANDBOX_ENV` | máquina da CLI / servidor | Nomes de variável de ambiente do lado do launcher, separados por vírgula, a injetar (`sandbox.cwsandbox.env` tem precedência no modo gerenciado) |
| `OMNICRAFT_CWSANDBOX_MAX_LIFETIME_S` | máquina da CLI / servidor | Teto do tempo de vida do sandbox em segundos (padrão 24h); também deriva o TTL do token de lançamento gerenciado |
| `OMNICRAFT_RUNNER_ENV_PASSTHROUGH` | dentro do sandbox (injetado) | Nomes de variável de ambiente extras que o host repassa para os runners |
| `GIT_TOKEN` / `GIT_USERNAME` | dentro do sandbox (injetado) | Credenciais HTTPS para clone / fetch / push de repositório privado |

## Smoke test

Valide as primitivas de API diretamente (sem precisar instalar o OmniCraft ou
o SDK — só stdlib + curl):

```bash
export CWSANDBOX_API_KEY=...
python tests/e2e/integrations/deploy/cwsandbox/smoke_test.py
```
