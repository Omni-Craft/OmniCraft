# Runners de sandbox do Kubernetes (Pods de host sob demanda)

Este overlay Kustomize liga o provider de sandbox gerenciado **`kubernetes`**:
uma sessão `host_type: managed` gera um **Pod runner** que roda `omnicraft
host` como comando do seu container e disca de volta para o servidor pelo
túnel de launch-token já existente. Ele empilha o RBAC + a configuração que o
provider precisa em cima do deployment base do servidor.

## Modelo de lançamento: entrypoint como host

O comando do container do Pod runner **é** o host. Um **init container**
prepara o workspace (`mkdir` + `git clone` opcional); o **container
principal** então roda `omnicraft host` sob um reaper minúsculo de PID 1. O
host reparenta os processos do runner para o PID 1, que o reaper coleta;
SIGTERM é repassado para um shutdown gracioso.

O launch token é entregue por um **Secret do Kubernetes por Pod**, referenciado
pelo `secretKeyRef` do Pod — ele nunca entra na spec do Pod, numa linha de
comando ou num audit log. O launcher cria esse Secret no provisionamento e o
apaga junto com o Pod ao terminar.

Como o host **nunca é iniciado dando `exec` num container já em execução**,
este provider não precisa de nenhuma concessão de `pods/exec` — e evita por
completo a classe de problemas de runtime do tipo exec-em-container-em-execução.
Os direitos da SA do servidor são o mínimo que o launcher chama: criar/obter/
apagar Pods, obter `pods/log` (só para diagnóstico de falha de início),
criar/apagar Secrets (o token por Pod), e listar events.

## Design de dois namespaces, com raio de impacto mínimo

| Namespace | Contém |
|---|---|
| `omnicraft` | o servidor, seu DB/PVC, seus Secrets, a SA `omnicraft-server` |
| `omnicraft-sandboxes` | os Pods runner, os Secrets de token por Pod, o Secret de credenciais do harness, a SA impotente `omnicraft-runner`, o Role + RoleBinding escopados |

Os direitos de Pod/Secret da SA do servidor são um **Role namespaced**
vinculado (entre namespaces) só a `omnicraft-sandboxes` — então um servidor
comprometido consegue gerenciar Pods runner, mas **não consegue** apagar os
Pods do servidor/DB, ler os Secrets do servidor, nem executar comandos dentro
de nenhum Pod. O namespace dos runners impõe Pod Security `restricted`; o Pod
runner gerado já é compatível com o restricted (uid 1000 não-root, remove
todas as caps com `ALL`, `seccompProfile: RuntimeDefault`, sem escalonamento
de privilégio).

## Pré-requisitos

1. **Uma imagem de servidor construída com o extra `kubernetes`.** O bloco
   `images:` do overlay já aponta para a variante oficial
   `omnicraft-server-kubernetes`, que já o inclui — nada para construir. Se
   você constrói a sua própria imagem, mantenha `kubernetes` em
   `OMNICRAFT_EXTRAS` (veja `deploy/docker`) ou o `_ensure_sdk()` falha em
   todo lançamento, e aponte `images:` para a sua build.
2. **Credenciais do harness.** Os runners leem as credenciais de LLM / git de
   um Secret nomeado por `secret_name` (padrão `omnicraft-creds`); você o cria
   fora de banda depois de aplicar o overlay — veja o passo 2 de **Aplique**.
   Ele deliberadamente não está versionado; para produção, prefira um Secret
   de sealed-secret / external-secrets.

## Aplique

```sh
# 1. RBAC, o namespace dos runners, a config de sandbox do servidor, e o patch do Deployment.
kubectl apply -k deploy/kubernetes/overlays/sandbox-runners

# 2. O Secret de credenciais do harness que os runners leem — criado fora de banda, como
#    o secret do OIDC em ../../README.md. Adicione só as chaves que seus agentes usam.
kubectl create secret generic omnicraft-creds -n omnicraft-sandboxes \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-... \
  --from-literal=OPENAI_API_KEY=sk-...
```

O passo 1 cria o namespace dos runners, as duas ServiceAccounts, o Role +
RoleBinding escopados, e a config `sandbox:` do servidor, e faz um patch no
Deployment do servidor para rodar como `omnicraft-server` com a config
montada. O passo 2 fornece as credenciais de modelo / git — veja
[Credenciais de modelo](#credenciais-de-modelo-chaves-de-llm) e
[Credenciais do Git](#credenciais-do-git-repositórios-privados) abaixo para
saber quais chaves definir (e um operador sealed-secret / external-secrets
para produção).

> **O Secret de `secret_name` precisa existir antes do primeiro lançamento
> gerenciado.** O `envFrom` dele não é opcional, então um Pod runner cujo
> Secret está faltando nunca inicia — ele fica parado em
> `CreateContainerConfigError` em vez de lançar sem credenciais. Crie-o
> (passo 2) logo após o `kubectl apply -k` do passo 1.

## Autenticação do servidor (hosts gerenciados)

Existem dois tipos de credencial aqui: a autenticação de **conexão com o
servidor** abaixo, e as chaves de **modelo** na próxima seção — mantenha-as
separadas.

Um sandbox gerenciado abre duas conexões de volta com o servidor. O **túnel
do host** é autenticado diretamente pelo token por lançamento — o Secret de
token por Pod, que sempre funciona. Mas o **túnel do runner** de cada sessão,
aberto pelo runner que o host gera, autentica com qualquer credencial de
*servidor* que consiga resolver — **não** o token do host. Então como você
coloca o servidor atrás de autenticação importa:

- **Autenticação via header / proxy OIDC, ou servidores single-user (sem
  autenticação)** — o túnel do runner não precisa de identidade extra; os
  hosts gerenciados funcionam de cara. (Verificado de ponta a ponta num
  servidor com autenticação por header: uma sessão `host_type: managed`
  lançou um Pod runner e rodou um turno do Claude usando um
  `CLAUDE_CODE_OAUTH_TOKEN` injetado.)
- **O provider embutido `accounts` (`OMNICRAFT_AUTH_ENABLED=1`)** — o túnel
  do runner exige, além disso, uma identidade de *usuário*, que o token do
  host por lançamento não carrega, então o dial-back do runner é recusado
  (`403`) mesmo com o túnel do host conectando. Esta é uma interação a nível
  de framework com hosts gerenciados, compartilhada por **todos** os
  providers de sandbox (Modal / Daytona / Islo / …), não específica do
  Kubernetes.

Então coloque o servidor atrás de **autenticação header ou OIDC** — um proxy
reverso / IdP injeta a identidade do usuário em toda requisição, incluindo o
WebSocket do runner (veja
[`deploy/README.md`](../../../README.md#autenticação)) — ou rode-o
single-user.

## Credenciais de modelo (chaves de LLM)

Um Pod runner novo não tem nenhuma chave de modelo. Elas viajam no **Secret
`omnicraft-creds`** (`secret_name`, projetado em todo Pod via `envFrom`)
criado em [Aplique](#aplique); o host dentro do sandbox repassa as variáveis
padrão de credencial do harness para os seus runners. Quais variáveis
injetar — APIs de primeira parte, gateways (`*_BASE_URL`), assinaturas — é
idêntico à Modal; veja a [tabela de variáveis e as receitas por
plano](../../../modal/README.md#credenciais-de-llm-para-sandboxes-gerenciados).
Para uma **assinatura** do Claude, rode `claude setup-token` na sua própria
máquina (autenticação única pelo navegador) e injete o token de vida longa
como `CLAUDE_CODE_OAUTH_TOKEN`. Para variáveis de ambiente além do conjunto
padrão do harness, defina também `OMNICRAFT_RUNNER_ENV_PASSTHROUGH=NAME1,NAME2`.

## Credenciais do Git (repositórios privados)

Injete um token HTTPS como `GIT_TOKEN` (GitLab: adicione
`GIT_USERNAME=oauth2`) no Secret `omnicraft-creds`. O helper de credencial git
da imagem do host responde pela autenticação HTTPS tanto para o clone no
lançamento quanto para o `fetch` / `push` posterior do agente, sem escrever
nada em disco — use URLs de repositório HTTPS. Os detalhes por provedor
combinam com o [guia de git da Modal](../../../modal/README.md#credenciais-do-git-repositórios-privados).

## Configuração (`sandbox-config.yaml`)

| Chave | Significado |
|---|---|
| `server_url` | URL para a qual o host do Pod runner disca de volta (DNS de service no cluster por padrão). |
| `namespace` | Namespace dos Pods runner (padrão `omnicraft-sandboxes`). |
| `secret_name` | Secret de credenciais do harness projetado em todo Pod via `envFrom`. |
| `service_account` | ServiceAccount na qual os Pods runner rodam (impotente). |
| `image` | Override opcional da imagem do runner (padrão: a imagem oficial multi-arch amd64/arm64 do host). |
| `env` | Lista opcional de nomes de variáveis de ambiente do SERVIDOR para injetar como env literal do Pod (prefira `secret_name` para credenciais). |
| `node_selector` | Labels de node extras opcionais, mescladas com um padrão `kubernetes.io/arch: amd64` — defina essa chave como `arm64` para agendar runners em nodes arm64. (nota arm64: o módulo de política CEL fica indisponível lá — o `cel-expr-python` não publica wheel aarch64 — e degrada de forma graciosa.) |
| `resources` | Override opcional de `requests` / `limits` (`cpu` / `memory`). |
| `in_cluster` | Fonte opcional de config do cluster: `true` (só SA no cluster), `false` (só kubeconfig), omitido (tenta no cluster, depois kubeconfig). |
| `kubeconfig` | Caminho opcional de kubeconfig para o fallback fora do cluster (env: `OMNICRAFT_KUBERNETES_KUBECONFIG`). |

## Resolução de problemas

- **O lançamento falha rápido com um motivo claro.** Quando um Pod não
  consegue ser agendado, puxar sua imagem, ou clonar seu repositório, o erro
  de lançamento carrega o diagnóstico — events recentes do Pod e um tail do
  log do container que falhou (ex.: o erro de `git clone` do init container).
  Sem precisar pegar o Pod antes dele ser coletado.
- **Inspecione um lançamento travado:** `kubectl describe pod <pod> -n
  omnicraft-sandboxes` e `kubectl logs <pod> -n omnicraft-sandboxes -c host`
  (ou `-c workspace-prep` para o passo de clone).
- **403 no lançamento:** a SA do servidor está sem o Role — reaplique este
  overlay e confirme que o namespace do subject do RoleBinding entre
  namespaces é `omnicraft`.
- **Pod runner travado em `CreateContainerConfigError`:** o Secret de
  `secret_name` (`omnicraft-creds`) não existe no namespace dos runners — o
  `envFrom` dele não é opcional, então o Pod não consegue iniciar. Crie-o
  (veja [Aplique](#aplique)).
- **O host fica online mas a sessão trava / dá 403 na primeira mensagem:** o
  servidor está usando o provider embutido `accounts`, que não suporta o
  dial-back do runner gerenciado — veja
  [Autenticação do servidor](#autenticação-do-servidor-hosts-gerenciados)
  (use autenticação header/OIDC, ou rode single-user).
- **401 / "could not load Kubernetes configuration":** fora do cluster, o
  servidor não consegue achar um kubeconfig — defina `kubeconfig` (ou
  `OMNICRAFT_KUBERNETES_KUBECONFIG`), ou remova `in_cluster: true` se ele não
  estiver de fato rodando no cluster.
