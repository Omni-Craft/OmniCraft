# OmniCraft no NVIDIA OpenShell

O [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) é um provedor de
sandbox auto-hospedado. O OmniCraft se conecta a um **gateway** OpenShell com
o SDK Python oficial [`openshell`](https://pypi.org/project/openshell/) e pede
para esse gateway criar, executar em, e apagar sandboxes no compute driver
configurado do gateway.

Este guia cobre a configuração do OpenShell específica do OmniCraft:

- instalar o extra `openshell`;
- selecionar um gateway OpenShell funcional;
- usar uma imagem de host do OmniCraft compatível com o OpenShell;
- configurar sandboxes lançados pela CLI ou gerenciados pelo servidor.

```bash
pip install 'omnicraft[openshell]'
```

O OmniCraft usa o OpenShell de duas formas:

- **Lançado pela CLI**: `omnicraft sandbox create` / `connect` provisiona um
  sandbox pelo seu terminal, envia o seu checkout local para dentro dele, e o
  registra como host no seu servidor.
- **Gerenciado pelo servidor**: o servidor provisiona um sandbox
  automaticamente quando uma sessão é criada com `"host_type": "managed"` e o
  termina quando a sessão é apagada.

Este é um guia de provedor de sandbox, não um alvo de deploy do servidor.

Duas características moldam o resto deste guia:

- **gRPC, e um gateway que você seleciona — não uma chave de API.** O
  OmniCraft se conecta pelo gateway OpenShell que você ativou com `openshell
  gateway select`. O `from_active_cluster()` do SDK resolve o endpoint, o
  material TLS e o token OIDC desse gateway a partir de `$OPENSHELL_GATEWAY` /
  `~/.config/openshell/active_gateway`. Não existe parâmetro de base-URL ou
  token no OmniCraft — a configuração do gateway e a autenticação são uma
  preocupação do OpenShell.
- **Sem port forward local.** O OpenShell não tem um caminho de callback
  sandbox→notebook, então o passo interativo `omnicraft login` / App OAuth
  dentro do sandbox é pulado automaticamente (como na Modal, na Daytona e no
  CoreWeave) — funciona bem para servidores com autenticação por token/OIDC.

## Pré-requisitos

Você precisa de um **gateway OpenShell rodando** com um compute driver,
ativado na máquina onde o launcher roda. Instalar e operar o gateway é uma
preocupação do OpenShell — siga a
[documentação do OpenShell](https://docs.nvidia.com/openshell). Instale o
runtime + CLI:

```bash
curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh
```

(No macOS Apple Silicon instala a fórmula do Homebrew; no Linux instala o
deb/rpm.)

> [!IMPORTANT]
> **O host do gateway precisa ser Linux amd64.** O supervisor do OpenShell
> (Landlock/seccomp/netns) não roda de forma confiável sob emulação — num
> host arm64 (ex.: Apple Silicon via colima) o sandbox nunca chega a READY. A
> imagem oficial do host agora publica multi-arch (amd64 + arm64), mas a
> variante arm64 dela omite o `cel-expr-python` (sem wheel linux-arm64 — as
> políticas CEL degradam para indisponíveis lá), então a variante amd64 é a
> que se deve rodar com o OpenShell. Num notebook Apple Silicon, aponte o
> gateway para uma máquina **Linux amd64** remota (e o servidor para esse
> gateway) em vez da VM Docker local.

### Gateway Docker local mínimo (para experimentar)

Para um teste local rápido, rode um gateway OpenShell apoiado no seu daemon
Docker local. O gateway precisa de uma chave de assinatura para que os
containers de sandbox consigam se autenticar de volta com ele; o script
auxiliar cria essa chave, escreve a config do gateway, inicia o gateway, o
registra na CLI do OpenShell, e espera o `openshell status` reportar
`Connected`.

Garanta que o Docker está rodando primeiro. Se você usa colima, defina
`DOCKER_HOST` antes de rodar o script:

```bash
export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock
```

Depois inicie e registre o gateway:

```bash
deploy/openshell/start-local-docker-gateway.sh
```

O script escreve o estado de desenvolvimento local em `~/.openshell-local` e
deixa os logs do gateway em `~/.openshell-local/gateway.log`.

Para um deploy de verdade, rode o gateway atrás de TLS com OIDC ou mTLS
(veja a documentação do OpenShell), depois `openshell gateway add
<https-url>` e `openshell gateway login`; o SDK pega o material de TLS/OIDC
dos metadados do gateway automaticamente — o OmniCraft não precisa de
configuração extra.

> [!WARNING]
> `allow_unauthenticated_users = true` e `--disable-tls` são conveniências
> de desenvolvimento local. Não exponha um gateway assim numa rede.

## A imagem do host

Os sandboxes inicializam a partir de
`ghcr.io/omnicraft-ai/omnicraft-host:latest`, publicada pela CI a partir do
alvo `host` do [`deploy/docker/Dockerfile`](../docker/Dockerfile), com o
OmniCraft e suas dependências pré-instaladas — incluindo as CLIs dos
harnesses de código (`claude`, `codex`, `pi`, `kiro-cli`), então agentes de
qualquer harness rodam sem instalação dentro do sandbox. O OpenShell injeta o
seu próprio supervisor como entrypoint do container.

O alvo `host` também carrega as duas coisas que o contrato de imagem do
OpenShell exige (e que são inertes para os providers baseados em root): um
**usuário/grupo `sandbox`** não-root e **`iproute2`/`nftables`** para o
namespace de rede por sandbox. Uma imagem customizada usada com o OpenShell
precisa incluir os dois, ou o supervisor se recusa a iniciar. (O launcher
cuida do detalhe não-root restante — fixando o cwd e o `$HOME` de cada exec
em `/home/sandbox` — então o padrão `/root` da imagem continua funcionando
para os outros providers.)

Antes de usar uma imagem com o OpenShell, teste esse contrato a partir do
mesmo daemon Docker que o gateway usa:

```bash
docker run --rm --entrypoint sh ghcr.io/omnicraft-ai/omnicraft-host:latest \
  -lc 'id sandbox && command -v ip && command -v nft'
```

Para usar uma imagem diferente (um fork, ou ferramentas extras embutidas),
rode a build a partir de um checkout do repositório do OmniCraft numa máquina
amd64 com Docker, depois envie para onde o driver do gateway conseguir
puxar:

```bash
docker build -f deploy/docker/Dockerfile --target host \
  --platform linux/amd64 \
  -t docker.io/<you>/omnicraft-host:latest .
docker push docker.io/<you>/omnicraft-host:latest
```

Depois aponte o OmniCraft para ela com `OMNICRAFT_OPENSHELL_HOST_IMAGE`.

> [!NOTE]
> **Isolado da internet (air-gapped)?** Pré-carregue a imagem do host (e a
> imagem do supervisor do OpenShell) no registry ou host de onde o gateway
> puxa — caso contrário, o primeiro lançamento a partir de uma imagem não
> cacheada espera por um pull do registry.

## Sandboxes lançados pela CLI

Com um gateway selecionado, provisione um sandbox e envie o seu checkout
local para dentro dele:

```bash
omnicraft sandbox create --provider openshell --server https://your-host
```

Isso cria um sandbox a partir da imagem do host, constrói wheels a partir do
seu checkout local, e as sobrepõe — então o sandbox roda o *seu* código, não
o que a imagem foi construída a partir de. Depois registre-o como host no seu
servidor:

```bash
omnicraft sandbox connect --provider openshell \
  --sandbox-id <id-printed-by-create> \
  --server https://your-host
```

O `connect` roda `omnicraft host` dentro do sandbox e mantém a conexão aberta
no seu terminal — Ctrl-C a derruba (parando o host dentro do sandbox).
Sessões novas apontando para aquele host agora rodam no sandbox. Passe um
`--host-name <label>` único por sandbox ao conectar vários a um servidor (o
servidor indexa hosts por (owner, name)). Sandboxes são descartáveis; quando
seu código muda, crie um novo.

Para injetar credenciais de LLM/git no sandbox, defina
`OMNICRAFT_OPENSHELL_SANDBOX_ENV` no seu shell como uma lista separada por
vírgulas de nomes de variáveis antes de rodar `create` — as variáveis
nomeadas são copiadas do seu ambiente para o sandbox no momento do
provisionamento. Um nome listado que **não** está definido faz o lançamento
falhar de forma clara (caso contrário isso apareceria bem mais tarde como uma
falha de autenticação opaca do harness dentro do sandbox):

```bash
export OMNICRAFT_OPENSHELL_SANDBOX_ENV=ANTHROPIC_API_KEY,GIT_TOKEN
omnicraft sandbox create --provider openshell --server https://your-host
```

## Sandboxes gerenciados pelo servidor

Adicione uma seção `sandbox:` na config do servidor (`omnicraft server -c
config.yaml`, ou `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: openshell
  server_url: https://your-host    # public URL sandboxes dial back to
```

`provider` + `server_url` é uma config completa. Sessões criadas com
`host_type: "managed"` (a chamada de API ou a opção New Sandbox da Web UI)
então rodam num sandbox OpenShell novo; o create retorna imediatamente e o
provisionamento acontece em segundo plano, exatamente como o
[fluxo gerenciado da Modal](../modal/README.md#sandboxes-gerenciados-pelo-servidor).
Cada sandbox gerenciado se autentica de volta com um token por lançamento
gerado pelo servidor — nenhuma credencial de usuário entra no sandbox para a
conexão com o servidor.

```bash
curl -X POST https://your-host/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "agent_...", "host_type": "managed"}'
```

Diferente dos provedores na nuvem, o OpenShell não precisa de chave de API no
ambiente do servidor — o **processo do servidor** precisa, em vez disso, de
acesso ao gateway OpenShell: ele se conecta com a mesma resolução
`from_active_cluster()` que a CLI, então selecione um gateway com `openshell
gateway select` (ou defina `OPENSHELL_GATEWAY` / `sandbox.openshell.cluster`)
onde o servidor roda. `server_url` precisa ser alcançável **a partir do
sandbox** — e como o OpenShell nega por padrão o egress, essa
alcançabilidade não é automática; veja
[Política de saída de rede](#política-de-saída-de-rede).

Configurações `openshell:` opcionais:

```yaml
sandbox:
  provider: openshell
  server_url: https://your-host
  openshell:
    image: docker.io/<you>/omnicraft-host:latest         # default: official image
    env: [OPENAI_API_KEY, ANTHROPIC_API_KEY, GIT_TOKEN]  # server env var NAMES to inject
    cluster: my-gateway                                  # default: active gateway
```

Como o dial-back gerenciado interage com o modo de autenticação do servidor é
um comportamento a nível de framework compartilhado por todos os providers;
veja
[`deploy/cwsandbox/README.md`](../cwsandbox/README.md#hosts-gerenciados-e-autenticação-do-servidor).

## Política de saída de rede

Esta é a parte de um deploy do OpenShell mais provável de te derrubar. O
OpenShell **nega por padrão**: todo sandbox roda no seu próprio namespace de
rede com todo o egress forçado por um proxy de política, e qualquer coisa não
explicitamente permitida é bloqueada (o `https_proxy` dentro do sandbox
retorna `403`). O agente e o host rodam *sem nenhum* acesso de saída até que
a política do sandbox conceda. A política é resolvida a partir de
`/etc/openshell/policy.yaml` embutido na imagem, ou definida por sandbox;
veja o [schema de política do OpenShell](https://docs.nvidia.com/openshell).
Um host gerenciado precisa de egress para:

- **a URL do servidor** (`server_url`) — o host e o runner discam de volta
  por um túnel WebSocket; sem ela o host consegue conectar mas o runner nunca
  se registra;
- **o host do provedor de LLM** — as chamadas de modelo do agente se
  originam *dentro* do sandbox (ex.: `*.googleapis.com` para o Gemini,
  `api.anthropic.com` para o Claude, `api.openai.com` para o OpenAI);
- **hosts de tokenizer/asset** que alguns harnesses buscam no primeiro uso,
  ex.: `*.blob.core.windows.net` (o harness openai-agents baixa o encoding
  `tiktoken`).

Um bloco `network_policies` mínimo (no `policy.yaml` da imagem) se parece
com:

```yaml
network_policies:
  server:
    endpoints: [{ host: "your-host.example.com", port: 443, tls: skip }]
    binaries:  [{ path: /** }]
  llm:
    endpoints: [{ host: "*.googleapis.com", port: 443, tls: skip }]
    binaries:  [{ path: /** }]
```

> [!IMPORTANT]
> **Repasse as variáveis de proxy para o runner.** O host herda o
> `https_proxy`/`http_proxy` do sandbox, mas o subprocesso do runner que ele
> gera **não** herda — então o runner falha com `Temporary failure in name
> resolution` mesmo com o host conectado. Injete `OMNICRAFT_RUNNER_ENV_PASSTHROUGH`
> nomeando as variáveis de proxy para que o host as repasse:
> ```yaml
> sandbox:
>   openshell:
>     env: [OMNICRAFT_RUNNER_ENV_PASSTHROUGH, …]   # value set in the server env:
> # OMNICRAFT_RUNNER_ENV_PASSTHROUGH=https_proxy,http_proxy,HTTPS_PROXY,HTTP_PROXY,NO_PROXY,no_proxy
> ```

> [!TIP]
> Para o tráfego de LLM especificamente, o OpenShell recomenda o seu
> **roteamento de inferência** em vez de colocar o host do provedor
> diretamente na allow-list, para que uma chave roubada não possa ser usada
> para alcançar o provedor de dentro do sandbox. A allow-list acima é o
> caminho mais simples para deixar um turno funcionando; o roteamento de
> inferência é o caminho hardened.

## Credenciais de modelo (chaves de LLM)

Um sandbox novo não tem nenhuma credencial de modelo. Nomeie as variáveis
para injetar em `OMNICRAFT_OPENSHELL_SANDBOX_ENV`; o launcher copia o valor
do seu ambiente para o sandbox, e o host dentro do sandbox repassa as
variáveis padrão de credencial do harness (`ANTHROPIC_API_KEY`,
`CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`,
`GEMINI_API_KEY`, …) para os seus runners.

```bash
export ANTHROPIC_API_KEY=sk-ant-…
export OMNICRAFT_OPENSHELL_SANDBOX_ENV=ANTHROPIC_API_KEY
```

Quais variáveis injetar — provedores, gateways, assinaturas — é idêntico aos
outros providers; veja a
[tabela de variáveis e as receitas por plano da Modal](../modal/README.md#credenciais-de-llm-para-sandboxes-gerenciados).
Para uma **assinatura** do Claude, rode `claude setup-token` na sua própria
máquina (autenticação única pelo navegador) e injete o `CLAUDE_CODE_OAUTH_TOKEN`
resultante. Para variáveis de ambiente além do conjunto padrão, injete
`OMNICRAFT_RUNNER_ENV_PASSTHROUGH=NAME1,NAME2`.

> [!TIP]
> O OpenShell também consegue impor política de credencial e egress na
> fronteira do sandbox, via sua política declarativa em YAML (um recurso do
> lado do gateway, independente do OmniCraft). Veja a
> [documentação de política do OpenShell](https://docs.nvidia.com/openshell).

## Credenciais do Git (repositórios privados)

Injete um token HTTPS como `GIT_TOKEN` (GitLab: adicione
`GIT_USERNAME=oauth2`) via `OMNICRAFT_OPENSHELL_SANDBOX_ENV`. O helper de
credencial git da imagem do host responde pela autenticação HTTPS tanto para
o clone no lançamento quanto para o `fetch` / `push` posterior do agente, sem
escrever nada em disco. Use URLs de repositório HTTPS. Os detalhes por
provedor combinam com o
[guia de git da Modal](../modal/README.md#credenciais-do-git-repositórios-privados).

## Como funciona

- **Conexão.** O `OpenShellSandboxLauncher` constrói um `SandboxClient` via
  `from_active_cluster()` e chama o gateway via gRPC: `CreateSandbox` +
  `wait_ready` para provisionar, `ExecSandbox` para rodar comandos,
  `DeleteSandbox` para terminar.
- **Envio de arquivos.** O OpenShell expõe execução de comandos mas nenhuma
  RPC de upload, então o `put` faz stream dos bytes do arquivo para o `cat`
  pelo stdin do canal de exec (a mesma abordagem que o próprio backend
  LangChain da NVIDIA usa). Wheels são enviadas dessa forma, depois
  instaladas com o comando compartilhado de overlay da imagem do host.
- **Identidade do sandbox.** O OpenShell atribui a cada sandbox um petname
  (ex.: `touched-urial`); esse nome é o identificador que o OmniCraft imprime
  e reusa. O `--name` pedido é apenas uma sugestão.
- **Execução não-root.** O OpenShell roda o agente como o usuário `sandbox`,
  então o launcher fixa o cwd e o `$HOME` de cada exec em `/home/sandbox` (a
  imagem mantém `/root` como padrão dela para os providers baseados em
  root).
- **Host de vida longa.** O OpenShell termina a árvore de processos de um
  exec no momento em que o exec retorna, então o host dentro do sandbox não
  pode ser destacado com o `setsid nohup … &` de sempre (ele é coletado
  instantaneamente). O launcher, em vez disso, o roda como um exec em
  foreground mantido aberto numa daemon thread pela vida da sessão.

## Resolução de problemas

- **`docker sandboxes require gateway JWT auth; configure [openshell.gateway.gateway_jwt]`**
  — o driver do Docker precisa de um sandbox JWT gerado pelo gateway. Gere o
  material de chave Ed25519 e adicione o bloco
  `[openshell.gateway.gateway_jwt]` como mostrado em
  [Gateway Docker local mínimo](#gateway-docker-local-mínimo-para-experimentar),
  depois reinicie o gateway.
- **`No OpenShell server configured` / `Could not connect to an OpenShell gateway`**
  — nenhum gateway está ativo. Rode `openshell gateway select <name>` (ou
  defina `OPENSHELL_GATEWAY`), e confirme com `openshell status`.
- **Sandbox travado em `Provisioning`** — geralmente um primeiro pull de
  imagem lento. Confirme que o daemon Docker do gateway consegue puxar a
  imagem do host (`docker pull <image>` a partir do mesmo `DOCKER_HOST`);
  pré-puxe para cachear. No colima, garanta que o gateway foi iniciado com
  `DOCKER_HOST` apontando para o socket do colima — o
  `/var/run/docker.sock` pode apontar para um Docker diferente (parado).
- **Agente sem credenciais** — confira se os nomes das variáveis injetadas
  combinam com o conjunto repassado (ou estão nomeadas em
  `OMNICRAFT_RUNNER_ENV_PASSTHROUGH`), e que cada nome foi de fato definido
  no ambiente de lançamento.
- **O host se registra mas o runner nunca fica online / o log do runner
  mostra `Temporary failure in name resolution`** — o subprocesso do runner
  não está recebendo as variáveis de proxy do sandbox. Repasse-as com
  `OMNICRAFT_RUNNER_ENV_PASSTHROUGH` (veja
  [Política de saída de rede](#política-de-saída-de-rede)).
- **O turno falha ao alcançar o modelo, ou o proxy retorna `403`** — o
  destino não está na allow-list de egress do sandbox. Adicione o host de
  LLM (e qualquer host de tokenizer/asset) a `network_policies` (veja
  [Política de saída de rede](#política-de-saída-de-rede)).
- **Container do sandbox reinicia / `sandbox user 'sandbox' not found` ou
  `trusted ip helper not found`** — a imagem não é compatível com o
  OpenShell. Use a imagem oficial do host (ou inclua o usuário `sandbox` +
  o `iproute2` na sua imagem customizada); veja
  [A imagem do host](#a-imagem-do-host).

## Referência de variáveis de ambiente

| Variável | Onde é lida | Propósito |
|---|---|---|
| `OPENSHELL_GATEWAY` | máquina da CLI / servidor | Nome do gateway a usar; sobrescreve `~/.config/openshell/active_gateway` (lido pelo SDK). `sandbox.openshell.cluster` tem precedência para o gerenciado. |
| `OMNICRAFT_OPENSHELL_HOST_IMAGE` | máquina da CLI | Sobrescreve a referência da imagem do host (padrão `ghcr.io/omnicraft-ai/omnicraft-host:latest`); `sandbox.openshell.image` é o equivalente gerenciado |
| `OMNICRAFT_OPENSHELL_SANDBOX_ENV` | máquina da CLI | Nomes de variáveis de ambiente do lado do launcher, separados por vírgula, para injetar no sandbox; `sandbox.openshell.env` é o equivalente gerenciado |
| `OMNICRAFT_RUNNER_ENV_PASSTHROUGH` | dentro do sandbox (injetada) | Nomes de variáveis de ambiente extras que o host repassa aos runners |
| `GIT_TOKEN` / `GIT_USERNAME` | dentro do sandbox (injetadas) | Credenciais HTTPS para clone / fetch / push de repositório privado |

## Validação

Exercitado de ponta a ponta contra um gateway OpenShell ao vivo num host
**Linux amd64** (driver Docker, a imagem oficial do host):

- **Primitivas do launcher** — provisionar → rodar (`echo` / `uname`) → put
  (upload de arquivo pelo stdin do exec) → verificar → terminar, além do
  `exec_foreground` (a primitiva do `connect`) fazendo stream da saída e
  propagando códigos de saída; os logs do gateway mostram as RPCs
  `CreateSandbox` / `ExecSandbox` / `DeleteSandbox` correspondentes.
- **Sessão completa gerenciada pelo servidor** — uma sessão
  `host_type:"managed"` levou o servidor a provisionar um sandbox no
  gateway, iniciar `omnicraft host` nele (exec em foreground mantido),
  discar de volta pelo túnel, se registrar, gerar o runner, e completar um
  turno de agente real (um modelo Gemini via o harness openai-agents) — a
  resposta do agente voltou de dentro do sandbox.

Testes unitários (um SDK/launcher falso, sem gateway necessário) cobrem
provisionamento, execução, upload de arquivo, streaming em foreground,
attach, terminação, passthrough de env, tratamento de erro, e o parsing da
config gerenciada:

```bash
pip install -e '.[openshell,dev]'
pytest tests/onboarding/sandboxes/test_openshell.py tests/server/test_managed_hosts.py
```
