# OmniCraft no Islo

Sandboxes do [Islo](https://islo.dev) dão a você machines de nuvem
descartáveis para rodar hosts do OmniCraft, de duas formas:

- **Lançado pela CLI**: `omnicraft sandbox create` / `connect` provisiona um
  sandbox a partir do seu terminal, envia seu checkout local para dentro
  dele, e o registra como host no seu servidor.
- **Gerenciado pelo servidor**: o servidor provisiona um sandbox
  automaticamente quando uma sessão é criada com `"host_type": "managed"` e
  o encerra quando a sessão é apagada.

Sandboxes sobem a partir da imagem de host pré-assada oficial, então a
inicialização leva segundos. Ao contrário da Modal e da Daytona, o lançador
do Islo conversa diretamente com a API HTTP do Islo através do `httpx` (já
uma dependência do OmniCraft), então **não há extra de SDK de provedor para
instalar** — só uma API key.

O que faz o Islo diferente dos outros provedores, e molda o resto deste
guia:

- **Um gateway de credenciais.** O Islo consegue injetar chaves de LLM/API
  no tráfego de saída de um sandbox na camada de rede, então a chave crua
  nunca chega ao processo do sandbox. Este é um caminho de primeira classe,
  recomendado, para credenciais de modelo (veja [Credenciais de
  modelo](#credenciais-de-modelo-chaves-de-llm)) e não tem equivalente na
  Modal ou na Daytona.
- **Sem port forward local.** O Islo não consegue encaminhar uma porta de
  callback sandbox→notebook, então o passo interativo de `omnicraft login`
  / App OAuth dentro do sandbox é pulado automaticamente (como na Modal e
  na Daytona).
- **Sem teto de lifetime.** Sandboxes do Islo rodam até serem apagados
  (como na Daytona, ao contrário das 24 h da Modal).

## Pré-requisitos

Instale a [CLI do Islo](https://docs.islo.dev) e crie uma API key, depois a
deixe disponível onde o lançador roda — seu shell para o fluxo de CLI, o
processo do **servidor** para sandboxes gerenciados:

```bash
curl -fsSL https://islo.dev/install.sh | sh   # instala a CLI do islo
islo login                                     # OAuth pelo navegador (uma vez)
islo api-key create omnicraft --show            # imprime um valor islo_key_…
export ISLO_API_KEY=islo_key_…
# Opcional: um endpoint de API não padrão
# export ISLO_BASE_URL=https://api.islo.dev
```

`ISLO_API_KEY` é trocado por um token de sessão de vida curta em `POST
/auth/token`; o token fica em cache até pouco antes de expirar. A chave é a
única credencial obrigatória — sem SDK, sem arquivo `~/.config`.

> [!NOTE]
> **O Islo não consegue encaminhar uma porta de callback local para dentro
> do sandbox.** O fluxo interativo de navegador do `omnicraft login` (e o
> callback de App OAuth dentro do sandbox) precisa de um port forward
> sandbox→notebook, que o Islo não fornece — então a CLI pula esse passo
> automaticamente, exatamente como faz para a Modal e a Daytona. Para um
> servidor que exige autenticação, injete as credenciais em vez disso (veja
> [Conectando-se a um servidor
> autenticado](#conectando-se-a-um-servidor-autenticado)).

## A imagem do host

Sandboxes sobem a partir de `ghcr.io/omnicraft-ai/omnicraft-host:latest`,
publicada pela CI a partir do alvo `host` do
[`deploy/docker/Dockerfile`](../docker/Dockerfile) com o OmniCraft e suas
dependências pré-instaladas — incluindo as CLIs de harness de código
(`claude`, `codex`, `pi`, `kiro-cli`), então agentes de qualquer harness
rodam sem uma instalação dentro do sandbox.

Para usar uma imagem diferente (um fork, ou ferramentas extras embutidas),
construa o mesmo alvo e envie-a para onde o Islo conseguir puxar:

```bash
docker build -f deploy/docker/Dockerfile --target host \
  --platform linux/amd64 \
  -t docker.io/<you>/omnicraft-host:latest .
docker push docker.io/<you>/omnicraft-host:latest
```

Depois aponte o OmniCraft para ela — `OMNICRAFT_ISLO_HOST_IMAGE` para o
fluxo de CLI, ou `sandbox.islo.image` na configuração do servidor para o
fluxo gerenciado. Para um registry privado, configure as credenciais de
pull do lado do Islo (é o Islo que puxa a imagem, não o OmniCraft).

> [!IMPORTANT]
> **Terminais nativos precisam de `bubblewrap`.** Os harnesses
> `claude-native` / `codex-native` / `kiro-native` / `pi` embrulham cada
> terminal de agente num OS-sandbox bubblewrap (`bwrap`), e no Linux esse
> isolamento é obrigatório e fail-loud — uma imagem de host sem o binário
> `bwrap` faz esses terminais falharem ao iniciar (`linux_bwrap sandbox
> requires the 'bwrap' binary on PATH`). O alvo `host` do Dockerfile instala
> o `bubblewrap`; se você trouxer sua própria imagem, instale-o lá também.
> Veja [Solução de problemas](#solução-de-problemas).

## Sandboxes lançados pela CLI

Provisione um sandbox e envie seu checkout local para dentro dele:

```bash
omnicraft sandbox create --provider islo
```

Isso puxa a imagem de host, constrói wheels a partir do seu checkout local,
e os sobrepõe por cima — então o sandbox roda *o seu* código, não o que
quer que a imagem tenha sido construída a partir de. Depois registre-o
como host no seu servidor:

```bash
omnicraft sandbox connect --provider islo \
  --sandbox-id <id-printed-by-create> \
  --server https://your-host
```

O `connect` roda `omnicraft host` dentro do sandbox e mantém a conexão
aberta no seu terminal — Ctrl-C a derruba. Novas sessões direcionadas a
esse host agora rodam no sandbox.

Rodando múltiplos sandboxes contra um servidor? Passe um `--host-name
<label>` único para cada `connect` — o servidor identifica hosts por
(owner, name), e sandboxes que compartilham um hostname colidem.

Sandboxes são descartáveis. Quando seu código muda, crie um novo — e apague
o antigo (sandboxes do Islo não têm teto de lifetime, então um sandbox
abandonado continua sendo cobrado até ser removido via `islo rm <id>` ou o
[dashboard](https://app.islo.dev)).

Para injetar credenciais de LLM/git num sandbox lançado pela CLI, defina
`OMNICRAFT_ISLO_SANDBOX_ENV` no seu shell como uma lista separada por
vírgulas de nomes de variáveis (ex.: `ANTHROPIC_API_KEY,GIT_TOKEN`) antes
de rodar o `create` — as variáveis nomeadas são copiadas do seu ambiente
para dentro do sandbox no momento do provisionamento. Um nome listado que
**não** está definido falha o lançamento de forma ruidosa (caso contrário,
apareceria muito mais tarde como uma falha opaca de autenticação de
harness dentro do sandbox).

### Conectando-se a um servidor autenticado

O `connect` roda `omnicraft host` dentro do sandbox, e esse host precisa
apresentar credenciais quando disca de volta para um servidor que exige
autenticação. O fluxo interativo de navegador do `omnicraft login` não
consegue rodar dentro de um sandbox do Islo (sem port forward de
callback), então injete as chaves para o servidor relevante em vez disso —
nomeie-as em `OMNICRAFT_ISLO_SANDBOX_ENV` antes do `create`:

```bash
export OMNICRAFT_ISLO_SANDBOX_ENV=DATABRICKS_HOST,DATABRICKS_TOKEN
omnicraft sandbox create --provider islo
```

O host dentro do sandbox gera um bearer token novo a partir dessas
credenciais em todo connect e reconnect. Para um servidor atrás do
Databricks, injete `DATABRICKS_HOST` mais `DATABRICKS_TOKEN` (um PAT) ou
`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` (um service principal
OAuth — regerar mantém um sandbox de vida longa conectado além da
expiração de qualquer token único).

Um servidor sem autenticação no túnel do host não precisa de nada disso, e
os [sandboxes gerenciados pelo servidor](#sandboxes-gerenciados-pelo-servidor)
também não — eles se autenticam automaticamente com um token por
lançamento gerado pelo servidor.

## Sandboxes gerenciados pelo servidor

Adicione uma seção `sandbox:` à configuração do servidor (`omnicraft
server -c config.yaml`, ou `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: islo
  server_url: https://your-host    # URL pública para onde os sandboxes discam de volta
```

`server_url` precisa ser alcançável *a partir da nuvem do Islo* — uma URL
HTTPS pública, não `localhost`. O próprio servidor precisa de
`ISLO_API_KEY` (e opcionalmente `ISLO_BASE_URL`) no seu ambiente. Sessões
criadas com `host_type: "managed"` (a chamada de API ou a opção New
Sandbox da Web UI) então rodam num sandbox novo do Islo; o create retorna
imediatamente e o provisionamento acontece em segundo plano, exatamente
como o [fluxo gerenciado da
Modal](../modal/README.md#sandboxes-gerenciados-pelo-servidor) — incluindo workspaces
de repositório, o rendezvous da primeira mensagem, e o relançamento de
sandbox morto.

```bash
curl -X POST https://your-host/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "agent_...", "host_type": "managed"}'
```

Cada sandbox gerenciado se autentica de volta com um token por lançamento
gerado pelo servidor (TTL de 7 dias — veja [Ciclo de
vida](#notas-de-ciclo-de-vida)); nenhuma credencial de usuário entra no
sandbox para a conexão com o servidor.

### Hosts gerenciados e autenticação do servidor

Como o dial-back se autentica depende de como o **servidor** faz a
autenticação, e há uma interação que vale a pena conhecer antes de você
publicar. Um sandbox gerenciado abre dois tipos de conexão de volta para o
servidor:

- o **túnel do host** (`/v1/hosts/<id>/tunnel`), que o token por
  lançamento autentica diretamente — o servidor o gera, o escopa a um
  único host, e resolve o dono a partir dele. Isso sempre funciona.
- um **túnel de runner** por sessão (`/v1/runners/<token>/tunnel`), aberto
  pelo subprocesso runner que o host lança. O runner se autentica com
  *qualquer credencial de servidor que consiga resolver* — uma identidade
  injetada por proxy (header / OIDC), ou um token de `omnicraft login`
  armazenado (só hosts locais; um sandbox gerenciado novo não tem nenhum)
  — **não** o token de host por lançamento.

A consequência:

- **Autenticação header / OIDC-proxy, ou servidores de usuário único (sem
  autenticação)** — o túnel de runner não precisa de identidade extra,
  então hosts gerenciados funcionam prontos para uso. **Verificado ponta a
  ponta num servidor de usuário único**: uma sessão criada com
  `host_type: "managed"` provisionou um sandbox do Islo a partir da imagem
  com bwrap, o lançador limpou o `apiKeyHelper` semeado, os túneis de host
  *e* de runner conectaram, e um terminal nativo do Claude rodou com a
  assinatura injetada via `CLAUDE_CODE_OAUTH_TOKEN`.
- **O provedor `accounts` embutido (`OMNICRAFT_AUTH_ENABLED=1`)** — o
  túnel de runner exige adicionalmente uma identidade de *usuário*, que o
  token de host por lançamento não carrega, então o dial-back do runner é
  recusado (`403`) mesmo com o túnel do host conectando. Esta é uma
  interação a nível de framework para hosts gerenciados, compartilhada
  por **todos** os provedores de sandbox (Modal / Daytona / Islo), não
  específica do Islo.

Então, para um deploy gerenciado do Islo, coloque o servidor atrás de
**autenticação header ou OIDC** (um reverse proxy / IdP injeta a
identidade do usuário em toda requisição, incluindo o WebSocket do
runner — veja [`deploy/README.md#autenticação`](../README.md#autenticação)), ou rode-o em
modo usuário único. O provedor `accounts` funciona bem para hosts lançados
pela CLI (você faz `omnicraft login`, e esse token é o que o host dentro
do sandbox repassa), mas ainda não para o dial-back de runner gerenciado.

Configurações opcionais de `islo:`:

```yaml
sandbox:
  provider: islo
  server_url: https://your-host
  islo:
    image: docker.io/<you>/omnicraft-host:latest   # padrão: imagem oficial
    env: [OPENAI_API_KEY, GIT_TOKEN]               # copia do ambiente do servidor
    base_url: https://api.islo.dev                 # endpoint de API não padrão
    gateway_profile: default                       # gateway do Islo para egress + injeção de credencial
    snapshot_name: warm-host                       # inicia a partir de um snapshot pré-assado
    workdir: /root/workspace                       # diretório de trabalho do sandbox
    vcpus: 2
    memory_mb: 4096
    disk_gb: 20
```

## Credenciais de modelo (chaves de LLM)

Um sandbox novo não tem nenhuma credencial de modelo sua. O Islo oferece
**duas formas distintas** de dar um modelo ao agente — e elas interagem,
então escolha uma deliberadamente por harness.

### Opção A — integração com o gateway do Islo (recomendado)

Este é o caminho nativo do Islo, sem equivalente na Modal/Daytona. Os
[gateways](https://docs.islo.dev/cli/gateways) do Islo "anexam
automaticamente API keys, tokens e secrets às requisições de saída" na
**camada de rede** — *"as credenciais nunca chegam ao processo do
sandbox."* Você conecta um provedor uma vez, do lado do servidor, e todo
sandbox a aproveita:

```bash
islo login --tool claude     # conecta a Anthropic via OAuth (apelido: --tool anthropic)
islo login --tool openai     # …e/ou a OpenAI
islo status                  # mostra as integrações conectadas
```

Isso **não é específico do Claude** — é como o Islo fornece credenciais de
modelo para *todo* harness. O Islo pré-semeia cada sandbox com uma chave
placeholder **phantom** (`islo_phantom_…`) em qualquer local que o
harness leia, por provedor:

| Harness | Local da chave phantom | Endpoint do provedor |
|---|---|---|
| Claude Code (`claude-native`, `claude-sdk`, `pi`) | `apiKeyHelper` em `~/.claude/settings.json` | `api.anthropic.com` |
| Codex / agentes OpenAI | variável de ambiente `OPENAI_API_KEY` | `api.openai.com` |

O harness envia esse placeholder para o endpoint do seu provedor; o
gateway intercepta a requisição e o troca pela sua credencial conectada
antes de encaminhar. A chave crua nunca chega ao sandbox, e a conexão vale
para o time inteiro — outros membros não precisam da própria. (Observamos
as duas chaves phantom pré-semeadas num único sandbox.)

Essas integrações conectam **API keys de provedor** (cobrança por token),
não autenticação de plano/assinatura — `--tool claude` dá uma API key da
Anthropic, não uma assinatura Claude Pro/Max; `--tool openai` dá uma API
key da OpenAI, não um plano ChatGPT. Para usar um token de assinatura ou
plano em qualquer harness (um token Claude Pro/Max, um token de acesso do
Codex), use a [Opção
B](#opção-b-injeção-de-variáveis-de-ambiente-do-omnicraft-sua-própria-chave-ou-uma-assinatura).

> [!IMPORTANT]
> Se `islo status` mostrar **"No integrations connected"** para um
> provedor, a chave phantom dele não resolve para nada — o harness cai
> numa requisição que falha (o Claude reporta "API Usage Billing" e tenta
> de novo). Conecte a integração para cada provedor cujo harness você usa.

#### Caminho A sob hosts gerenciados

É aqui que o gateway brilha: quando o **servidor** lança sandboxes, você
não configura **nenhuma credencial de modelo do lado do OmniCraft**. O
fluxo:

```
admin (once):  islo login --tool claude   → connects Anthropic to the Islo ACCOUNT
                                                              │
server ──ISLO_API_KEY──▶ Islo API "create sandbox" ──▶ sandbox under that account
                                                              │ Islo pre-seeds the
                                                              │ phantom apiKeyHelper
                                                              ▼
   agent's claude → api.anthropic.com (phantom key) ──▶ Islo gateway swaps in the real key
```

O servidor do OmniCraft só guarda `ISLO_API_KEY` — a credencial que ele
usa para *criar* sandboxes. Como todo sandbox gerenciado é criado sob
aquela conta do Islo, e as integrações são conectadas no nível de
**conta/time**, cada um herda a credencial do Claude conectada através do
gateway automaticamente. O único controle do lado do OmniCraft é qual
gateway um sandbox gerenciado usa:

```yaml
sandbox:
  provider: islo
  server_url: https://your-host
  islo:
    gateway_profile: default     # o gateway do Islo que carrega a integração conectada
```

Duas consequências que vale a pena internalizar:

- **Nenhum secret de modelo mora na configuração ou no ambiente do
  servidor do OmniCraft** — nada para vazar ali. Compare com a [Opção B
  sob hosts
  gerenciados](#opção-b-injeção-de-variáveis-de-ambiente-do-omnicraft-sua-própria-chave-ou-uma-assinatura),
  onde a chave fica em `sandbox.islo.env` (copiada do ambiente do
  servidor para cada sandbox).
- **A integração precisa estar conectada na mesma conta do Islo à qual o
  `ISLO_API_KEY` do servidor pertence.** Se o seu servidor roda sob uma
  conta de serviço/CI dedicada do Islo, rode `islo login --tool claude`
  autenticado como *aquela* conta — não um login pessoal de notebook.

### Opção B — injeção de variáveis de ambiente do OmniCraft (sua própria chave ou uma assinatura)

Traga sua própria credencial nomeando-a em `OMNICRAFT_ISLO_SANDBOX_ENV`
(CLI) ou `sandbox.islo.env` (gerenciado); o lançador copia o valor do
ambiente que lança para dentro do sandbox, e o host dentro do sandbox
repassa as variáveis padrão de credencial de harness para seus runners:

| Variável | Habilita |
|---|---|
| `ANTHROPIC_API_KEY` | Modelos Claude na API da Anthropic |
| `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` | Gateways compatíveis com Anthropic (LiteLLM, bridges Bedrock/Vertex, proxies corporativos) |
| `CLAUDE_CODE_OAUTH_TOKEN` | claude-code com uma **assinatura** Claude (sem API key) |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | OpenAI ou qualquer endpoint compatível com OpenAI (OpenRouter, vLLM, Ollama, …) |
| `CODEX_ACCESS_TOKEN` | codex com um workspace ChatGPT Business/Enterprise |
| `GEMINI_API_KEY` | Gemini na API do Google AI |

As receitas completas por plano (assinaturas, gateways, modelos
open-source) são idênticas às da Modal — veja a [tabela de variáveis e as
receitas](../modal/README.md#credenciais-de-llm-para-sandboxes-gerenciados). Para
uma **assinatura** Claude especificamente, rode `claude setup-token` na
sua própria máquina (autenticação única via navegador) e injete o token
de vida longa resultante como `CLAUDE_CODE_OAUTH_TOKEN`. Para variáveis de
ambiente além do conjunto padrão, injete
`OMNICRAFT_RUNNER_ENV_PASSTHROUGH=NAME1,NAME2`.

> [!NOTE]
> **Sua credencial Claude injetada automaticamente prevalece sobre o
> helper phantom do Islo.** O Islo semeia um `apiKeyHelper` em todo
> sandbox, e o Claude Code normalmente o preferiria em vez de um
> `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` no ambiente. Então, quando
> você injeta um desses, o lançador do Islo remove o `apiKeyHelper`
> semeado no momento do provisionamento — para **ambos** os sandboxes,
> lançados pela CLI e gerenciados pelo servidor — deixando sua credencial
> como o único caminho de autenticação. Nenhum passo manual, nada para
> rodar dentro do sandbox.

Codex/OpenAI também não precisa de nada especial — o phantom dele é a
variável de ambiente `OPENAI_API_KEY`, que o seu `OPENAI_API_KEY` injetado
simplesmente sobrescreve.

### Escolhendo entre A e B

| | Opção A — gateway | Opção B — injeção de variáveis de ambiente |
|---|---|---|
| Credencial | **API key** de provedor (Anthropic, OpenAI, …) | sua API key **ou** token de assinatura/plano |
| Cobrança | API por token | API key, ou sua assinatura/plano |
| Chave no sandbox? | **Não** (o gateway injeta fora de banda) | Sim (no ambiente do sandbox) |
| Escopo | conta/time inteiro, todos os sandboxes | por sandbox |
| Configuração para host gerenciado | só `gateway_profile`; nenhum secret no servidor | chave em `sandbox.islo.env` |
| Melhor para | **gerenciado / produção**, cobrança por API key | sua própria **assinatura** ou chave; CLI ou gerenciado |
| Pegadinha | precisa de uma integração conectada | a variável injetada precisa estar definida onde o lançador roda |

Escolha **uma por harness** — conecte a integração do Islo *ou* injete
sua própria credencial, nunca as duas. As duas funcionam em qualquer
fluxo de lançamento: o lançador remove o `apiKeyHelper` semeado do Islo
automaticamente quando você injeta uma credencial Claude. Os mesmos dois
padrões se aplicam ao Codex/OpenAI (`islo login --tool openai`, ou injete
`OPENAI_API_KEY` / `CODEX_ACCESS_TOKEN`).

### Credenciais de git (repositórios privados)

Injete um token HTTPS como `GIT_TOKEN` (GitLab: adicione
`GIT_USERNAME=oauth2`) via `OMNICRAFT_ISLO_SANDBOX_ENV` /
`sandbox.islo.env`. O credential helper de git da imagem de host responde
à autenticação HTTPS a partir dele, tanto para o clone no momento do
lançamento quanto para os `fetch` / `push` posteriores do agente, sem
escrever nada em disco. Use URLs de repositório HTTPS. Detalhes por
provedor batem com o [guia de git da
Modal](../modal/README.md#credenciais-do-git-repositórios-privados).

## Considerações de segurança

- **O Caminho A mantém a chave de modelo fora do sandbox — uma vantagem
  real.** Com o gateway, o processo do agente só vê o placeholder
  phantom; a chave real é injetada na borda de rede do Islo. Um agente
  comprometido ou vítima de prompt injection não consegue exfiltrar uma
  chave que nunca segurou. Esta é a postura de credencial mais forte dos
  três provedores para chaves de modelo, e a razão para preferir a Opção A
  onde a cobrança por API key for aceitável.
- **O gateway termina o TLS para injetar.** A injeção de credencial
  significa que o gateway do Islo fica no caminho do tráfego de LLM de
  saída do agente e o reorigina — então esse tráfego (prompts,
  completions, saída de ferramenta enviada ao modelo) fica visível na
  borda do Islo. Aceitável para a maioria dos times, mas pese isso para
  cargas de trabalho altamente sensíveis, e escope o `gateway_profile`
  exatamente para o egress que você pretende.
- **A Opção B coloca a chave no sandbox.** Um token de assinatura ou API
  key nomeado em `sandbox.islo.env` é copiado para o ambiente do sandbox
  no momento do provisionamento e vive ali pela vida do sandbox. Prefira
  credenciais escopadas e de vida curta, e conte com o sandbox `bwrap`
  por terminal (abaixo) para manter o agente longe dela.
- **O terminal do agente fica isolado desses secrets.** Terminais de
  harness nativo rodam sob um OS-sandbox bubblewrap que mascara dotfiles
  (`~/.ssh`, `~/.aws`, o token do servidor `~/.omnicraft` injetado) e
  prende o agente ao seu workspace — defesa em profundidade *dentro* do
  sandbox do Islo, independente do isolamento próprio do Islo. É por isso
  que a imagem precisa trazer o `bwrap` (veja [a imagem do
  host](#a-imagem-do-host)).
- **Todos os sandboxes gerenciados compartilham uma org + `ISLO_API_KEY`
  do Islo.** O isolamento entre usuários depende das fronteiras de
  sandbox do Islo, e a chave de org compartilhada consegue enumerar e
  apagar qualquer sandbox — a mesma forma de org single-tenant dos
  provedores Modal e Daytona. Escope a org para essa carga de trabalho.
- **O lifetime do launch token é de 7 dias.** Sandboxes do Islo não têm
  teto de lifetime de plataforma, então o token de host por lançamento
  precisa sobreviver a um sandbox de longa duração entre reconexões (uma
  janela de replay maior que as ~24 h da Modal; igual à Daytona). Um
  relançamento gera um novo.

## Notas de ciclo de vida

- **Sem teto de lifetime de plataforma.** Ao contrário do limite de 24
  horas da Modal, sandboxes do Islo rodam até serem apagados. O fluxo
  gerenciado apaga um sandbox quando sua sessão é apagada, e o caminho de
  relançamento de sandbox morto substitui um que crashou ou foi removido
  fora de banda. Sandboxes lançados pela CLI você apaga você mesmo (`islo
  rm <id>`).
- **Recursos.** Sandboxes usam por padrão 2 vCPUs e 4 GiB de memória;
  sobrescreva por lançamento gerenciado com `vcpus` / `memory_mb` /
  `disk_gb`.
- **Warm starts.** Defina `sandbox.islo.snapshot_name` para iniciar a
  partir de um snapshot pré-assado do Islo em vez de um pull de imagem a
  frio.
- **Ciclo de vida do lado do provedor** (list / status / delete / stop) —
  use a CLI `islo` (`islo ls`, `islo rm <id>`) ou o
  [dashboard](https://app.islo.dev) diretamente.

## Custo

O Islo cobra por uso, sem licenças por assento nem taxas de ociosidade:
~$0.07/hora de CPU, ~$0.04/hora de GB de memória, ~$0.0007/hora de GB de
disco — cerca de $0.25/hora para o sandbox padrão de 2 vCPU / 4 GiB
enquanto ele roda. Contas novas ganham $50 de créditos grátis. Tarifas:
[islo.dev](https://islo.dev).

## Solução de problemas

- **Terminal nativo do Claude/Codex falha com `linux_bwrap sandbox
  requires the 'bwrap' binary on PATH`.** Os harnesses nativos embrulham
  cada terminal de agente num OS-sandbox bubblewrap; a imagem de host
  precisa trazer o `bubblewrap`. O alvo `host` do Dockerfile o instala —
  reconstrua a partir de uma imagem atual, ou, para um caso avulso num
  sandbox lançado pela CLI, rode `apt-get install -y bubblewrap` dentro
  dele.
- **O Claude mostra "API Usage Billing" / "both `CLAUDE_CODE_OAUTH_TOKEN`
  and `apiKeyHelper` set".** Você injetou sua própria credencial Claude
  (Opção B) mas o `apiKeyHelper` phantom do Islo ainda está presente — o
  lançador o remove automaticamente no provisionamento, então isso
  significa que a remoção não rodou: confirme que a credencial está
  nomeada em `OMNICRAFT_ISLO_SANDBOX_ENV` / `sandbox.islo.env` (o sinal
  que o lançador usa), e verifique o log de provisionamento pela linha
  "clearing Islo's seeded apiKeyHelper".
- **Requisições tentam de novo e depois falham sem erro óbvio.** O `islo
  status` mostra nenhuma integração conectada, então o `apiKeyHelper`
  phantom não resolve para nada. Conecte uma (Opção A) ou troque para a
  Opção B.
- **"managed host did not come online within 120s".** Confira que
  `server_url` é publicamente alcançável a partir da nuvem do Islo, depois
  inspecione o log do host dentro do sandbox:
  `~/.omnicraft/logs/host-runner/*.log`.
- **Agente não tem credenciais.** Verifique se os nomes das variáveis
  injetadas batem com o conjunto repassado acima (ou estão nomeados em
  `OMNICRAFT_RUNNER_ENV_PASSTHROUGH`), e que cada nome foi de fato
  definido no ambiente que lança.

## Referência de variáveis de ambiente

| Variável | Onde é lida | Finalidade |
|---|---|---|
| `ISLO_API_KEY` | machine da CLI / servidor | Credenciais de API do Islo (obrigatória) |
| `ISLO_BASE_URL` | machine da CLI / servidor | Endpoint de API do Islo não padrão (padrão `https://api.islo.dev`) |
| `OMNICRAFT_ISLO_HOST_IMAGE` | machine da CLI / servidor | Sobrescreve a referência da imagem de host (`sandbox.islo.image` tem precedência para gerenciados) |
| `OMNICRAFT_ISLO_SANDBOX_ENV` | machine da CLI / servidor | Nomes de variáveis de ambiente do lado do lançador a injetar, separados por vírgulas (`sandbox.islo.env` tem precedência para gerenciados) |
| `OMNICRAFT_RUNNER_ENV_PASSTHROUGH` | dentro do sandbox (injetada) | Nomes extras de variáveis de ambiente que o host repassa aos runners |
| `GIT_TOKEN` / `GIT_USERNAME` | dentro do sandbox (injetada) | Credenciais HTTPS para clone / fetch / push de repositório privado |
