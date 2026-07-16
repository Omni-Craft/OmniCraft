# OmniCraft no E2B

Sandboxes do [E2B](https://e2b.dev) dão a você machines de nuvem descartáveis
para rodar hosts do OmniCraft, de duas formas:

- **Lançado pela CLI**: `omnicraft sandbox create` / `connect` provisiona um
  sandbox a partir do seu terminal, envia seu checkout local para dentro dele,
  e o registra como host no seu servidor.
- **Gerenciado pelo servidor**: o servidor provisiona um sandbox
  automaticamente quando uma sessão é criada com `"host_type": "managed"` e o
  encerra quando a sessão é apagada.

> [!IMPORTANT]
> **O E2B sobe a partir de um *template* pré-construído, não de uma imagem de
> registry.** Ao contrário dos lançadores da Modal / Daytona / CoreWeave —
> que baixam `ghcr.io/omnicraft-ai/omnicraft-host` diretamente — o E2B não
> consegue iniciar uma imagem de registry arbitrária no momento da criação.
> Você precisa primeiro construir a imagem de host do OmniCraft num template
> do E2B (um passo único, abaixo); o campo `template` do lançador então nomeia
> *aquele template*, não uma referência `ghcr.io/...`. Essa é a única
> diferença real em relação aos outros provedores de sandbox. Este diretório
> **não** é um destino de deploy do servidor.

## Pré-requisitos

```bash
pip install 'omnicraft[e2b]'   # instala o extra do SDK do e2b
npm i -g @e2b/cli             # a CLI do E2B, para construir o template
```

Crie uma API key no [dashboard do E2B](https://e2b.dev/dashboard) e a deixe
disponível onde o lançador roda — seu shell para o fluxo de CLI, o processo
do **servidor** para sandboxes gerenciados:

```bash
export E2B_API_KEY=e2b_…
e2b auth login                # único, também autentica a CLI do E2B
```

> [!NOTE]
> **O lifetime tem um teto e não pode ser desativado.** Um sandbox do E2B
> carrega um único timeout (padrão de 5 minutos; máximo de conta **24 h no
> Pro, 1 h no Hobby**) sem opção de "nunca expirar". O OmniCraft solicita o
> máximo de 24 h na criação, mas o E2B **rejeita** (não limita) uma
> solicitação acima do teto da conta, então o `provision` automaticamente
> **tenta de novo limitado ao máximo da conta** (ex.: 1 h no Hobby) —
> verificado ao vivo. Defina `OMNICRAFT_E2B_MAX_LIFETIME_S` para solicitar um
> lifetime específico e pular a nova tentativa. Uma sessão gerenciada que
> ultrapassa o teto depende do caminho de relançamento de sandbox morto
> (mesma postura do limite de 24 h da Modal), então uma **conta Pro** é
> recomendada para qualquer coisa além de demos curtas.

## Construa o template do host (uma vez)

O E2B constrói um template a partir de um Dockerfile cuja imagem base deve
ser **baseada em Debian** e **single-stage**. A imagem de host do OmniCraft
(`python:slim`, Debian) satisfaz as duas condições — então o Dockerfile do
template é uma linha só, sem nenhuma camada em cima da imagem publicada:

```bash
mkdir -p omnicraft-e2b && cd omnicraft-e2b
cat > e2b.Dockerfile <<'EOF'
# Single-stage, baseado em Debian — os dois requisitos do E2B. A imagem de
# host já traz a instalação completa do omnicraft mais git / tmux / curl,
# então nada mais é necessário aqui.
FROM ghcr.io/omnicraft-ai/omnicraft-host:latest
EOF

e2b template build --name omnicraft-host --dockerfile e2b.Dockerfile
```

`omnicraft-host` é o nome de template padrão que o lançador procura
([`DEFAULT_E2B_TEMPLATE`](../../omnicraft/onboarding/sandboxes/e2b.py)),
então um deployment que use esse nome não precisa de nenhuma configuração
adicional. Use um nome diferente (ou fixe uma imagem de host `:sha-<short>`)
e aponte o lançador para ela com `sandbox.e2b.template` /
`OMNICRAFT_E2B_TEMPLATE`.

Para rodar sua própria imagem de host, construa o alvo `host` de
[`deploy/docker/Dockerfile`](../docker/Dockerfile) (`--platform
linux/amd64`), envie-a para onde o E2B conseguir puxar, e use `FROM` para
essa referência em `e2b.Dockerfile` em vez disso. Reconstrua o template
sempre que a imagem de host mudar (o fluxo de CLI ainda sobrepõe seus wheels
*locais* por cima por sandbox, então mudanças de código do dia a dia não
exigem reconstruir o template).

## Sandboxes lançados pela CLI

Provisione um sandbox e envie seu checkout local para dentro dele:

```bash
omnicraft sandbox create --provider e2b
```

Isso inicia um sandbox a partir do template `omnicraft-host`, constrói
wheels a partir do seu checkout local, e os sobrepõe por cima — então o
sandbox roda *o seu* código, não o que quer que o template tenha sido
construído a partir de. Depois registre-o como host no seu servidor:

```bash
omnicraft sandbox connect --provider e2b \
  --sandbox-id <id-printed-by-create> \
  --server https://your-host
```

O `connect` roda `omnicraft host` dentro do sandbox e mantém a conexão
aberta no seu terminal — Ctrl-C a derruba (e mata o processo remoto; o E2B
expõe um kill handle de verdade). Novas sessões direcionadas a esse host
agora rodam no sandbox.

Rodando múltiplos sandboxes contra um servidor? Passe um `--host-name
<label>` único para cada `connect` — o servidor identifica hosts por
(owner, name), e sandboxes que compartilham um hostname colidem.

Sandboxes são descartáveis. Quando seu código muda, crie um novo — e apague
o antigo (pelo [dashboard](https://e2b.dev/dashboard) ou `e2b sandbox kill
<id>`), embora o E2B também o colha automaticamente no seu timeout.

Para injetar credenciais de LLM/git num sandbox lançado pela CLI, defina
`OMNICRAFT_E2B_SANDBOX_ENV` no seu shell como uma lista separada por
vírgulas de nomes de variáveis (ex.: `ANTHROPIC_API_KEY,GIT_TOKEN`) antes de
rodar o `create` — as variáveis nomeadas são copiadas do seu ambiente para
dentro do sandbox no momento do provisionamento.

> [!NOTE]
> O E2B não tem port forward local→sandbox (ele expõe portas do sandbox
> *para fora* só via URLs públicas). O passo interativo de `omnicraft login`
> / App OAuth dentro do sandbox é, portanto, pulado automaticamente (como
> na Modal / Daytona): use o E2B com servidores que não exigem autenticação
> de App dentro do sandbox, ou autentique via credenciais injetadas
> (abaixo).

## Sandboxes gerenciados pelo servidor

Adicione uma seção `sandbox:` à configuração do servidor (`omnicraft server
-c config.yaml`, ou `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: e2b
  server_url: https://your-host    # URL pública para onde os sandboxes discam de volta
```

`server_url` precisa ser alcançável *a partir da nuvem do E2B* — uma URL
HTTPS pública, não `localhost`. Sessões criadas com `host_type: "managed"`
(a chamada de API ou a opção New Sandbox da Web UI) então rodam num sandbox
novo do E2B; o create retorna imediatamente e o provisionamento acontece em
segundo plano, exatamente como o [fluxo gerenciado da
Modal](../modal/README.md#sandboxes-gerenciados-pelo-servidor) — incluindo workspaces
de repositório, o rendezvous da primeira mensagem, e o relançamento de
sandbox morto.

Configurações opcionais de `e2b:`:

```yaml
sandbox:
  provider: e2b
  server_url: https://your-host
  e2b:
    template: omnicraft-host          # NOME do template do E2B (padrão: omnicraft-host)
    env: [OPENAI_API_KEY, ANTHROPIC_API_KEY, GIT_TOKEN]
```

> [!NOTE]
> `sandbox.e2b.template` é um **nome de template do E2B** (construído
> acima), não uma referência de imagem de registry — o campo que guarda uma
> referência `ghcr.io/...` nos outros provedores. Omita-o para usar o
> template padrão `omnicraft-host`.

## Credenciais para o sandbox (chaves de LLM, tokens de git)

`sandbox.e2b.env` lista os **nomes** das variáveis a copiar do **próprio
ambiente do servidor** para dentro de todo sandbox no momento do
provisionamento (passadas para `Sandbox.create(envs=…)`). Os valores nunca
ficam no arquivo de configuração — defina-os onde o servidor roda:

```bash
export OPENAI_API_KEY=sk-…       # no servidor
export GIT_TOKEN=github_pat_…    # clone/fetch/push de repositório privado
```

```yaml
sandbox:
  provider: e2b
  server_url: https://your-host
  e2b:
    env: [OPENAI_API_KEY, GIT_TOKEN]
```

Um nome listado que **não** está definido no ambiente do servidor falha o
lançamento de forma ruidosa (caso contrário, apareceria muito mais tarde
como uma falha opaca de autenticação de harness dentro do sandbox).

Quais variáveis injetar — provedores, gateways, assinaturas, git — é
idêntico à Modal; veja a [tabela de variáveis e as receitas por
plano](../modal/README.md#credenciais-de-llm-para-sandboxes-gerenciados) e as
[credenciais de git](../modal/README.md#credenciais-do-git-repositórios-privados).
O host dentro do sandbox repassa o mesmo conjunto padrão para seus runners,
e `OMNICRAFT_RUNNER_ENV_PASSTHROUGH` (como uma variável injetada) nomeia
quaisquer extras.

A mesma injeção de variáveis de ambiente também carrega **credenciais para
se conectar ao próprio servidor**, para um host que autentica seu dial-back
com credenciais de usuário em vez de um launch token. Lançamentos
gerenciados nunca precisam disso: o servidor injeta um launch token por
lançamento automaticamente. Mas um host [lançado pela
CLI](#sandboxes-lançados-pela-cli) precisa quando o servidor exige
autenticação — nomeie as chaves (ex.: `DATABRICKS_HOST` +
`DATABRICKS_TOKEN`) em `OMNICRAFT_E2B_SANDBOX_ENV` antes do `create`. Veja
[Conectando-se a um servidor
autenticado](../modal/README.md#conectando-a-um-servidor-autenticado) no
guia da Modal.

## Considerações de segurança

- **Credenciais injetadas chegam ao control plane do E2B.** Os valores de
  `sandbox.e2b.env` são enviados à API do E2B como variáveis de ambiente
  literais do sandbox. Prefira credenciais **escopadas e de vida curta**:
  um PAT granular limitado aos repositórios que uma sessão precisa, um
  token de gateway em vez de uma chave raiz de provedor. (O lançador da
  Modal anexa secrets nomeados da Modal, então seus valores ficam no
  secret store da Modal — uma postura mais forte; o mesmo trade-off do
  provedor Daytona.)
- **Todos os sandboxes gerenciados compartilham uma conta + API key do
  E2B.** O isolamento entre usuários do OmniCraft depende das fronteiras de
  sandbox do E2B, e a chave compartilhada consegue enumerar e matar o
  sandbox de qualquer usuário. Escope a conta para essa carga de trabalho.
- **O lifetime do launch token é de ~25 h, derivado do lifetime
  *solicitado*.** Sandboxes do E2B compartilham o teto rígido de 24 h da
  Modal, então o launch token por lançamento sobrevive ao sandbox por mais
  uma hora para reautenticar o túnel entre reconexões. O TTL é calculado a
  partir de `OMNICRAFT_E2B_MAX_LIFETIME_S` (padrão 24 h) na inicialização
  do servidor, então ele limita o sandbox *mais longo possível*. Numa conta
  com teto onde o `provision` limita o sandbox a algo mais curto (ex.: 1 h
  no Hobby), o token cobre mais do que o necessário para o sandbox agora
  mais curto — seguro para reautenticação, mas um token vazado é
  reproduzível pela janela inteira. Para reduzir isso, **defina
  `OMNICRAFT_E2B_MAX_LIFETIME_S` para o teto da sua conta** para que o TTL
  do token acompanhe o lifetime concedido (ou defina um `token_ttl_s` mais
  curto num `ManagedSandboxConfig` construído diretamente). Um relançamento
  gera um token novo.
- **URLs de sandbox são públicas por padrão.** O E2B expõe portas do
  sandbox via URLs públicas `*.e2b.app`; o OmniCraft nunca abre uma (o host
  disca *para fora*, para o seu servidor), mas fique atento a que nada num
  sandbox deveria vincular um serviço esperando que ele seja privado sem o
  controle de acesso por token do E2B.

## Solução de problemas

- **"E2B sandbox creation failed: template '…' is unavailable"** — a imagem
  de host nunca foi construída num template do E2B, ou o nome não bate.
  Rode a [construção do template](#construa-o-template-do-host-uma-vez)
  com `--name omnicraft-host` (ou defina `sandbox.e2b.template` com o seu
  nome).
- **"managed host did not come online within 120s"** — o sandbox não
  conseguiu discar de volta para `server_url`. Confirme que é uma URL HTTPS
  pública alcançável a partir da nuvem do E2B (não `localhost`), e confira
  `/tmp/omnicraft-host.log` dentro do sandbox.
- **Sandbox para depois de ~1 hora** — você está numa conta Hobby (teto de
  1 h); o `provision` se ajusta automaticamente a ele (você verá um aviso
  de uma linha). Faça upgrade para o Pro para o máximo de 24 h, ou espere o
  caminho de relançamento de sandbox morto reprovisionar na próxima
  mensagem.

## Notas de ciclo de vida

- **Teto rígido de lifetime, sem desativar a parada por ociosidade.** O
  `provision` solicita `OMNICRAFT_E2B_MAX_LIFETIME_S` (padrão o máximo de
  24 h do Pro); o E2B rejeita uma solicitação acima do teto da conta, então
  a criação tenta de novo limitada a ele (ex.: 1 h no Hobby). O
  `keep_alive` reestende um sandbox vivo na reconexão, mas não há opção de
  nunca expirar — uma sessão gerenciada além do teto é substituída pelo
  caminho de relançamento de sandbox morto (igual à Modal).
- **Templates, não imagens de registry.** Veja [Construa o template do
  host](#construa-o-template-do-host-uma-vez). Recursos (vCPU / memória)
  são fixados quando o template é construído — passe `--cpu-count` /
  `--memory-mb` para `e2b template build` — não no momento da criação do
  sandbox.
- **Imagens customizadas** exigem reconstruir o template: use `FROM` para
  sua imagem em `e2b.Dockerfile` e rode `e2b template build` nela, depois
  defina `sandbox.e2b.template` / `OMNICRAFT_E2B_TEMPLATE`.

## Referência de variáveis de ambiente

| Variável | Onde é lida | Finalidade |
|---|---|---|
| `E2B_API_KEY` | machine da CLI / servidor | Credenciais de API do E2B (obrigatória) |
| `OMNICRAFT_E2B_TEMPLATE` | machine da CLI / servidor | Nome do template do E2B a partir do qual provisionar (`sandbox.e2b.template` tem precedência; padrão `omnicraft-host`) |
| `OMNICRAFT_E2B_SANDBOX_ENV` | machine da CLI / servidor | Nomes de variáveis de ambiente do lado do lançador a injetar, separados por vírgulas (`sandbox.e2b.env` tem precedência para gerenciados) |
| `OMNICRAFT_E2B_MAX_LIFETIME_S` | machine da CLI / servidor | Lifetime de sandbox solicitado em segundos (padrão 24 h); a criação se ajusta automaticamente ao teto da conta se excedido |
