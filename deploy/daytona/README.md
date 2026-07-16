# OmniCraft no Daytona

Os sandboxes do [Daytona](https://www.daytona.io) te dão máquinas de nuvem
descartáveis para rodar hosts do OmniCraft, de duas formas:

- **Lançado pela CLI**: `omnicraft sandbox create` / `connect` provisiona um
  sandbox a partir do seu terminal, envia o seu checkout local para dentro
  dele, e o registra como host no seu servidor.
- **Gerenciado pelo servidor**: o servidor provisiona um sandbox
  automaticamente quando uma sessão é criada com `"host_type": "managed"` e o
  encerra quando a sessão é apagada.

Os sandboxes inicializam a partir da imagem oficial pré-pronta do host, então
o startup leva segundos assim que o Daytona já colocou a imagem em cache como
um snapshot interno — o primeiríssimo lançamento a partir de uma imagem leva
alguns minutos enquanto o Daytona puxa e tira o snapshot dela.

> Este diretório também contém o código-fonte do **relay de egress do tier
> gratuito** (`wrangler.toml`, `src/index.js`) — um Cloudflare Worker que
> deixa sandboxes Daytona Tier 1/2 alcançarem o seu servidor através do
> firewall de egress do Daytona. Veja
> [Configuração do relay do tier gratuito](#configuração-do-relay-do-tier-gratuito-tier-12).
> Ele NÃO é um destino de deploy do servidor.

## Pré-requisitos

```bash
pip install 'omnicraft[daytona]'   # instala o extra do SDK do daytona
```

> [!IMPORTANT]
> **O egress no Daytona é allowlisted, o que molda como você roda hosts
> (lançados pela CLI e gerenciados, os dois).** Organizações Daytona
> [Tier 1/2](https://www.daytona.io/docs/en/limits/) permitem tráfego de
> saída só para uma
> [allowlist fixa](https://www.daytona.io/docs/en/network-limits) de
> domínios públicos (hosts de git, gerenciadores de pacote, as APIs dos
> principais providers de IA) que admins da org **não conseguem modificar**.
> Duas consequências:
>
> 1. A discagem de volta do host dentro do sandbox para o seu `server_url`
>    do OmniCraft é bloqueada a menos que essa URL esteja na allowlist —
>    senão o lançamento dá timeout com "managed host did not come online".
> 2. As chamadas de LLM do agente só funcionam contra um **endpoint de
>    modelo allowlisted** (`api.openai.com`, `api.anthropic.com`, …). Um
>    endpoint privado ou de gateway é bloqueado da mesma forma.
>
> **Duas formas de resolver isso:**
>
> - **Tier 3+** (um *top-up* de uso de $500 — crédito de sandbox pré-pago,
>   não uma taxa) remove a restrição de egress por completo: aponte
>   `server_url` para o seu servidor de verdade e use qualquer endpoint de
>   modelo, sem relay. Melhor para times que já estão no Daytona; a postura
>   de segurança mais limpa (TLS ponta a ponta, sem middlebox).
> - **Tier gratuito (Tier 1/2) via um relay allowlisted** — `*.workers.dev`
>   passa pelo firewall, então um Cloudflare Worker pequeno que faz reverse
>   proxy para o seu servidor deixa a discagem de volta passar; roteie
>   qualquer endpoint de modelo não allowlisted por um segundo Worker da
>   mesma forma. **Verificado funcionando de ponta a ponta no Tier 1.** Isso
>   insere um middlebox que termina TLS, então leia
>   [Considerações de segurança](#considerações-de-segurança) primeiro. Veja
>   [Configuração do relay do tier gratuito](#configuração-do-relay-do-tier-gratuito-tier-12)
>   abaixo.
>
> Se você está avaliando sandboxes de nuvem do zero e não quer rodar um
> relay, o [Modal](../modal/README.md#sandboxes-para-hosts-de-runner) tem egress
> total no seu tier de entrada.

Crie uma chave de API no [painel do Daytona](https://app.daytona.io)
(Dashboard → Keys) e deixe-a disponível onde o launcher roda — o seu shell
para o fluxo de CLI, o processo do **servidor** para sandboxes gerenciados:

```bash
export DAYTONA_API_KEY=dtn_…
# Opcional: um endpoint de API ou região de destino não padrão
# export DAYTONA_API_URL=https://app.daytona.io/api
# export DAYTONA_TARGET=us
```

## Sandboxes lançados pela CLI

Provisione um sandbox e envie o seu checkout local para dentro dele:

```bash
omnicraft sandbox create --provider daytona
```

Isso puxa a imagem do host, constrói wheels a partir do seu checkout local, e
as sobrepõe — então o sandbox roda *o seu* código, não o que a imagem foi
construída a partir de. Depois registre-o como host no seu servidor:

```bash
omnicraft sandbox connect --provider daytona \
  --sandbox-id <id-printed-by-create> \
  --server https://your-host
```

O `connect` roda o `omnicraft host` dentro do sandbox (via uma sessão PTY) e
mantém a conexão aberta no seu terminal — Ctrl-C derruba tudo. Novas sessões
apontando para esse host agora rodam no sandbox.

Rodando vários sandboxes contra um servidor? Passe um `--host-name <label>`
único para cada `connect` — o servidor indexa hosts por (dono, nome), e
sandboxes que compartilham um nome colidem.

Sandboxes são descartáveis. Quando o seu código mudar, crie um novo — e
apague o antigo (sandboxes Daytona não têm teto de tempo de vida, e o fluxo
de CLI desliga o auto-stop por ociosidade, então sandboxes abandonados
continuam sendo cobrados até serem removidos pelo
[painel](https://app.daytona.io) ou por `daytona sandbox delete`).

> [!NOTE]
> Em organizações no tier gratuito (Tier 1/2) a URL de `--server` precisa
> passar pela allowlist de egress ou o `omnicraft host` dentro do sandbox não
> consegue discar de volta — veja a nota sobre tiers acima e a
> [configuração do relay](#configuração-do-relay-do-tier-gratuito-tier-12).

Para injetar credenciais de LLM/git num sandbox lançado pela CLI, defina
`OMNICRAFT_DAYTONA_SANDBOX_ENV` no seu shell com uma lista de nomes de
variável separados por vírgula (ex.: `ANTHROPIC_API_KEY,GIT_TOKEN`) antes de
rodar `create` — as variáveis nomeadas são copiadas do seu ambiente para
dentro do sandbox no momento do provisionamento.

## Sandboxes gerenciados pelo servidor

Adicione uma seção `sandbox:` na configuração do servidor (`omnicraft server
-c config.yaml`, ou `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: daytona
  server_url: https://your-host    # URL pública para onde os sandboxes discam de volta
```

`server_url` precisa ser alcançável *a partir da nuvem do Daytona* — uma URL
HTTPS pública, não `localhost`. Sessões criadas com `host_type: "managed"` (a
chamada de API ou a opção New Sandbox da Web UI) rodam então num sandbox
Daytona novo; o create retorna imediatamente e o provisionamento acontece em
segundo plano, exatamente como o [fluxo gerenciado do
Modal](../modal/README.md#sandboxes-gerenciados-pelo-servidor) — incluindo workspaces
de repositório, o rendezvous da primeira mensagem, e o relançamento de
sandbox morto.

Configurações `daytona:` opcionais:

```yaml
sandbox:
  provider: daytona
  server_url: https://your-host
  daytona:
    image: docker.io/<you>/omnicraft-host:latest  # padrão: a imagem oficial
    env: [OPENAI_API_KEY, ANTHROPIC_API_KEY, GIT_TOKEN]
```

## Credenciais para o sandbox (chaves de LLM, tokens de git)

O Daytona não tem um cofre de secret nomeado do lado do provider para anexar
na criação do sandbox, então as credenciais são injetadas como variáveis de
ambiente: `sandbox.daytona.env` lista os **nomes** das variáveis a copiar do
**próprio ambiente do servidor** para dentro de cada sandbox no momento do
provisionamento. Os valores nunca ficam no arquivo de configuração — defina-
os onde o servidor roda:

```bash
export OPENAI_API_KEY=sk-…       # no servidor
export GIT_TOKEN=github_pat_…    # clone/fetch/push de repositório privado
```

```yaml
sandbox:
  provider: daytona
  server_url: https://your-host
  daytona:
    env: [OPENAI_API_KEY, GIT_TOKEN]
```

Um nome listado que **não** está definido no ambiente do servidor faz o
lançamento falhar ruidosamente (senão isso apareceria bem mais tarde como uma
falha opaca de autenticação do harness dentro do sandbox).

Quais variáveis injetar — providers, gateways, assinaturas, git — é idêntico
ao Modal; veja a [tabela de variáveis e as receitas por
plano](../modal/README.md#credenciais-de-llm-para-sandboxes-gerenciados) e as
[credenciais de git](../modal/README.md#credenciais-do-git-repositórios-privados).
O host dentro do sandbox repassa o mesmo conjunto padrão para os seus
runners, e `OMNICRAFT_RUNNER_ENV_PASSTHROUGH` (como uma variável injetada)
nomeia quaisquer extras.

A mesma injeção de env também carrega **credenciais para conectar ao próprio
servidor**, para um host que autentica a sua discagem de volta com
credenciais de usuário em vez de um token de lançamento. Lançamentos
gerenciados nunca precisam disso: o servidor injeta um token de host por
lançamento automaticamente. Mas um host
[lançado pela CLI](#sandboxes-lançados-pela-cli) precisa quando o servidor
exige autenticação — injete as chaves do servidor em questão, ex.:
`DATABRICKS_HOST` + `DATABRICKS_TOKEN` (ou `DATABRICKS_CLIENT_ID` /
`DATABRICKS_CLIENT_SECRET`) para um servidor por trás do Databricks, nomeando-
as em `OMNICRAFT_DAYTONA_SANDBOX_ENV` antes do `create` — e o host dentro do
sandbox gera bearer tokens novos a partir delas a cada reconexão. Veja
[Conectando a um servidor
autenticado](../modal/README.md#conectando-a-um-servidor-autenticado) no
guia do Modal.

> [!NOTE]
> No **tier gratuito**, o endpoint de modelo do agente também precisa estar
> na allowlist (`api.openai.com`, `api.anthropic.com`, …). Um endpoint
> privado ou de gateway fica atrás do firewall — roteie-o por um segundo
> relay (veja abaixo) e injete a URL `*.workers.dev` do relay como
> `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL`.

## Configuração do relay do tier gratuito (Tier 1/2)

Sandboxes Daytona no tier gratuito (Tier 1/2) só conseguem alcançar um
[conjunto de domínios allowlisted](https://www.daytona.io/docs/en/network-limits);
`*.workers.dev` está nele. O Cloudflare Worker pronto para deploy neste
diretório mora lá e faz reverse proxy de forma transparente de toda
requisição — HTTP puro e upgrades de WebSocket — para o seu servidor OmniCraft
de verdade, então a discagem de volta de um host gerenciado (o WS do túnel de
host, o WS do túnel de runner, e HTTP puro) alcança o servidor através do
firewall.

```bash
npm i -g wrangler          # ou use npx
wrangler login             # uma vez, grátis, sem cartão de crédito
cd deploy/daytona
wrangler deploy --var UPSTREAM_URL:https://your-omnicraft-server
# → https://omnicraft-daytona-relay.<your-subdomain>.workers.dev
```

Aponte `sandbox.daytona.server_url` para a URL `*.workers.dev` impressa. Para
um endpoint de modelo não allowlisted, publique uma segunda cópia (`name =
"omnicraft-llm-relay"`, `UPSTREAM_URL` = o seu gateway) e injete a URL dela
como `OPENAI_BASE_URL` via `sandbox.daytona.env`.

**Este caminho é verificado de ponta a ponta numa org Daytona Tier 1 de
verdade** (create gerenciado → discagem de volta do host através do relay →
runner → turno de LLM de verdade → teardown). Leia o trade-off de segurança
abaixo antes de depender dele.

## Considerações de segurança

- **Credenciais injetadas vivem no control plane do Daytona.** O Daytona não
  tem cofre de secret nomeado, então os valores de `sandbox.daytona.env` são
  enviados para a API do Daytona como variáveis de ambiente literais do
  sandbox e guardados nos metadados do sandbox — um terceiro agora detém
  tudo que você injetar (chaves de LLM, `GIT_TOKEN`). Prefira credenciais
  **com escopo restrito e de vida curta**: um PAT granular limitado aos
  repositórios que uma sessão precisa, um token de gateway em vez de uma
  chave raiz do provider. (O launcher do Modal anexa secrets nomeados do
  Modal em vez disso, então os valores dele ficam no cofre de secrets do
  Modal — uma postura mais forte; essa é a principal diferença de segurança
  entre os dois providers.)
- **Todos os sandboxes gerenciados compartilham uma org Daytona + uma chave
  de API.** O isolamento entre usuários do OmniCraft se apoia inteiramente
  nas fronteiras de sandbox do Daytona, e a chave de org compartilhada
  consegue enumerar e apagar o sandbox de qualquer usuário. Mesma forma de
  org de tenant único do provider Modal; restrinja a org só a essa carga de
  trabalho.
- **O tempo de vida do token de lançamento é de 7 dias.** Sandboxes Daytona
  não têm teto de tempo de vida da plataforma, então o token de host por
  lançamento precisa sobreviver a um sandbox de longa duração através de
  reconexões de túnel — uma janela maior que as ~25h do Modal. Um token
  vazado é reutilizável contra o servidor por essa janela; um relançamento
  gera um novo. Deploys que injetam o seu próprio launcher podem definir um
  `token_ttl_s` mais curto no `ManagedSandboxConfig` se os seus sandboxes
  forem de vida curta.
- **A solução de contorno do relay Tier 1/2 é um MITM que termina TLS.** Um
  relay num domínio coringa allowlisted (`*.vercel.app` / `*.workers.dev`)
  precisa ser um serviço L7 — ele termina o TLS e reorigina, então vê o
  token de lançamento do host e todo o payload do túnel (frames de runner,
  saída de ferramentas, conteúdo de arquivos) em texto puro na sua borda. Use
  só um relay que você controla totalmente, com logging desligado; nunca um
  compartilhado/público. O caminho de egress direto (Tier 3) mantém o túnel
  com TLS ponta a ponta sem middlebox nenhum e é a escolha certa para
  qualquer deploy sensível à segurança.

## Troubleshooting

- **"managed host did not come online within 120s"** — em organizações Tier
  1/2 isso quase sempre é o firewall de egress bloqueando a discagem de
  volta do host para `server_url` (veja a nota sobre tiers acima). Verifique
  com `curl <server_url>/health` dentro de um sandbox. No Tier 3+, cheque o
  `/tmp/omnicraft-host.log` dentro do sandbox.
- **Primeiro lançamento lento** — o create inicial a partir de uma imagem
  nova constrói um snapshot do Daytona (minutos); lançamentos seguintes são
  em segundos.
- **"Organization is suspended: Please verify your email address"** —
  complete a verificação de email no
  [painel](https://app.daytona.io/dashboard/limits) (cadastro via SSO do
  GitHub/Google chega pré-verificado).

## Notas de ciclo de vida

- **Sem teto de tempo de vida da plataforma.** Diferente do limite de 24
  horas do Modal, sandboxes Daytona rodam até serem apagados. O OmniCraft
  desliga o auto-stop por ociosidade de 15 minutos do Daytona no momento do
  provisionamento (um host de sessão precisa sobreviver a intervalos entre
  turnos); o sandbox é apagado quando a sua sessão é apagada, e o caminho de
  relançamento de sandbox morto substitui um que quebrou ou foi apagado fora
  de banda.
- **O primeiro lançamento por imagem é lento.** O Daytona constrói um
  snapshot interno a partir da imagem no primeiro uso (minutos para a imagem
  de host de ~1,4 GiB); lançamentos seguintes o reaproveitam (segundos).
- **Imagens customizadas** funcionam como as do Modal: construa o alvo
  `host` do [`deploy/docker/Dockerfile`](../docker/Dockerfile)
  (`--platform linux/amd64`) e envie para qualquer registry de onde o
  Daytona possa puxar, depois defina `sandbox.daytona.image` ou
  `OMNICRAFT_DAYTONA_HOST_IMAGE`.

## Referência de variáveis de ambiente

| Variável | Onde é lida | Finalidade |
|---|---|---|
| `DAYTONA_API_KEY` | máquina da CLI / servidor | Credenciais de API do Daytona (obrigatório) |
| `DAYTONA_API_URL` | máquina da CLI / servidor | Endpoint de API do Daytona não padrão |
| `DAYTONA_TARGET` | máquina da CLI / servidor | Região de destino para sandboxes novos |
| `OMNICRAFT_DAYTONA_HOST_IMAGE` | máquina da CLI / servidor | Sobrescreve a referência da imagem do host (`sandbox.daytona.image` tem precedência) |
| `OMNICRAFT_DAYTONA_SANDBOX_ENV` | máquina da CLI / servidor | Nomes de variável de ambiente do lado do launcher, separados por vírgula, a injetar (`sandbox.daytona.env` tem precedência no modo gerenciado) |
