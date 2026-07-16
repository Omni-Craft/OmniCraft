# OmniCraft no Kubernetes

Faça o deploy do OmniCraft em qualquer cluster Kubernetes usando Kustomize. Os
manifests puxam a imagem pré-construída e configuram um volume persistente e
health checks. Eles também incluem um Ingress para você servir o app via HTTPS
num endereço web público, mas essa parte é opcional — só importa quando as
pessoas precisam alcançar o servidor pela internet, e ela traz dois add-ons
extras (ingress-nginx e cert-manager). Para uso local ou de desenvolvimento,
ignore isso e conecte com `kubectl port-forward` (veja
[Verifique o deploy](#verifique-o-deploy)).

## O que é provisionado

- **Deployment** — um pod de réplica única rodando
  `ghcr.io/omnicraft-ai/omnicraft-server`, servido na porta 8000.
- **Service** — ClusterIP na porta 80 → 8000.
- **Ingress** *(opcional)* — serve o app via HTTPS num endereço web público,
  usando o cert-manager para o certificado. Pule se o servidor não vai para a
  internet.
- **PVC** — volume de 10 Gi em `/data/artifacts` para o artifact store, o
  cookie secret gerado e as credenciais de admin.
- **ConfigMap + Secret** — configuração de ambiente e credenciais do banco de
  dados.

## Pré-requisitos

- Um cluster Kubernetes (1.25+)
- `kubectl` com suporte a Kustomize (`kubectl kustomize` ou o `kustomize`
  standalone)
- Um banco PostgreSQL (gerenciado ou no próprio cluster — veja abaixo)
- *Só se você for colocar o servidor num endereço web público:* um controlador
  de ingress (ex.: ingress-nginx) e o cert-manager

### Instale os add-ons do cluster para ingress e gerenciamento de certificados (opcional)

Pule isto a menos que você vá colocar o servidor num endereço web público.
(Para uso local ou de desenvolvimento você vai alcançá-lo com `kubectl
port-forward`, ou pode deixar seu próprio load balancer ou proxy cuidar do
HTTPS.) Caso contrário, se o seu cluster ainda não tem um controlador de
ingress e o cert-manager, instale os dois (fixe as versões a gosto):

```bash
# ingress-nginx — use o manifest do provedor que combina com o seu cluster
# (este é o do kind; para EKS/GKE/AKS use o manifest ou o Helm chart daquele provedor):
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml

# cert-manager:
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml

# aguarde os dois ficarem prontos:
kubectl wait -n ingress-nginx --for=condition=Ready pod \
  -l app.kubernetes.io/component=controller --timeout=180s
kubectl wait -n cert-manager --for=condition=Available deployment --all --timeout=180s
```

### Crie um issuer do cert-manager (opcional)

Pule isto a menos que você esteja usando o Ingress. O cert-manager busca o
certificado HTTPS do Ingress a partir de um `ClusterIssuer` chamado
`letsencrypt-prod` (a anotação `cert-manager.io/cluster-issuer` em
`base/ingress.yaml`). Esse issuer **não** é entregue aqui — crie um antes de
fazer o deploy, ou troque a anotação para um issuer que você já tenha. Duas
escolhas comuns:

```yaml
# Produção — certificados reais do Let's Encrypt
# (precisa de um domínio público e de um Ingress alcançável pela internet):
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: you@example.com
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
      - http01:
          ingress:
            ingressClassName: nginx
```

```yaml
# Local / dev — autoassinado (não precisa de DNS público; navegadores vão avisar):
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  selfSigned: {}
```

Aplique o issuer escolhido com `kubectl apply -f <file>`. Sem isso, o
cert-manager registra `IssuerNotFound` e nenhum certificado é emitido (o
servidor continua rodando — só o TLS é afetado).

## Deploy com um banco de dados externo

Use este caminho quando você tem um Postgres gerenciado (RDS, Cloud SQL, Neon,
etc.).

1. **Edite o secret** — defina a sua `DATABASE_URL` real e gere um cookie
   secret:

   ```bash
   # deploy/kubernetes/base/secret.yaml
   DATABASE_URL: "postgresql+psycopg://user:pass@your-db-host:5432/omnicraft"
   OMNICRAFT_ACCOUNTS_COOKIE_SECRET: "$(openssl rand -hex 32)"
   ```

2. **Defina o seu domínio** *(pule se você não estiver usando o Ingress)* —
   troque `omnicraft.example.com` em `base/ingress.yaml` pelo seu domínio, e
   garanta que o `ClusterIssuer` `letsencrypt-prod` existe (veja
   [Crie um issuer do cert-manager](#crie-um-issuer-do-cert-manager-opcional)).

3. **Aplique:**

   ```bash
   kubectl kustomize deploy/kubernetes/base/ | kubectl apply -f -
   ```

4. **Crie o primeiro admin.** Abra o app (pelo host do seu Ingress, ou com
   port-forward para uma checagem rápida — veja
   [Verifique o deploy](#verifique-o-deploy)). Com o provider `accounts`
   padrão, o primeiro visitante reivindica a instância: a tela de Setup pede
   um usuário + senha, e quem terminar primeiro vira o admin.

## Deploy com Postgres dentro do cluster

O overlay `overlays/postgres/` adiciona um StatefulSet de Postgres 16 de
réplica única, com seu próprio PVC de 10 Gi. Bom para clusters de
dev/teste.

1. **Edite os secrets** — em `overlays/postgres/secret-patch.yaml`, troque
   `changeme` por senhas reais:

   ```bash
   POSTGRES_PASSWORD: "<strong-password>"
   DATABASE_URL: "postgresql+psycopg://omnicraft:<strong-password>@postgres:5432/omnicraft"
   OMNICRAFT_ACCOUNTS_COOKIE_SECRET: "$(openssl rand -hex 32)"
   ```

2. **Defina o seu domínio** *(pule se você não estiver usando o Ingress)* —
   edite o hostname em `base/ingress.yaml`, e garanta que o `ClusterIssuer`
   `letsencrypt-prod` existe (veja
   [Crie um issuer do cert-manager](#crie-um-issuer-do-cert-manager-opcional)).

3. **Aplique:**

   ```bash
   kubectl kustomize deploy/kubernetes/overlays/postgres/ | kubectl apply -f -
   ```

## Deploy com sandboxes OpenShell

O overlay `overlays/openshell/` configura o servidor para provisionar
sandboxes do [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) para
sessões gerenciadas, e inclui o RBAC para a CRD
[kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
quando o gateway usa um compute driver Kubernetes.

1. **Edite o patch do configmap** — defina `OMNICRAFT_SANDBOX_SERVER_URL` como
   a URL pública para a qual os sandboxes vão discar de volta, e, opcionalmente,
   defina `OPENSHELL_GATEWAY` como o nome de um gateway específico:

   ```bash
   # deploy/kubernetes/overlays/openshell/configmap-patch.yaml
   OMNICRAFT_SANDBOX_SERVER_URL: "https://omnicraft.example.com"
   OPENSHELL_GATEWAY: "my-gateway"
   ```

2. **Edite os secrets** — em `overlays/openshell/secret-patch.yaml`, defina a
   URL do banco de dados, o cookie secret e as chaves de API de LLM que o seu
   harness precisa:

   ```bash
   DATABASE_URL: "postgresql+psycopg://omnicraft:<password>@your-db-host:5432/omnicraft"
   OMNICRAFT_ACCOUNTS_COOKIE_SECRET: "$(openssl rand -hex 32)"
   ANTHROPIC_API_KEY: "sk-ant-..."
   ```

3. **Acesso ao gateway** — o pod do servidor precisa alcançar o endpoint gRPC
   do gateway OpenShell. Se o gateway roda dentro do cluster, garanta que o
   NetworkPolicy permite isso (a política incluída permite todo o egress na
   443 — restrinja a gosto). Se o gateway guarda a config/material TLS num
   Secret, crie `openshell-gateway-config` no namespace `omnicraft`, e o
   deployment monta ele em `~/.config/openshell`.

4. **Instale a CRD agent-sandbox** *(opcional)* — se o gateway OpenShell
   delega ao controller kubernetes-sigs/agent-sandbox:

   ```bash
   kubectl apply -f https://raw.githubusercontent.com/kubernetes-sigs/agent-sandbox/main/config/crd/bases/sandbox.agent.k8s.io_agentsandboxes.yaml
   ```

   O RBAC do overlay já concede à ServiceAccount do servidor permissão para
   gerenciar recursos `AgentSandbox`.

5. **Aplique:**

   ```bash
   kubectl kustomize deploy/kubernetes/overlays/openshell/ | kubectl apply -f -
   ```

Para OpenShell + Postgres dentro do cluster, empilhe o overlay do postgres por
cima (componha as duas bases numa kustomization nova, ou aplique o StatefulSet
do postgres separadamente). Veja
[Política de saída de rede](../openshell/README.md#política-de-saída-de-rede)
para a allow-list de egress do lado do sandbox (URL do servidor + hosts do
provedor de LLM).

## Construindo uma imagem UBI (Red Hat / OpenShift)

Para ambientes RHEL e OpenShift que exigem containers compatíveis com UBI, use
a variante UBI do Dockerfile. Ela usa a Red Hat Universal Base Image 9
(`ubi9/python-312`, `ubi9/nodejs-20`) e roda o servidor como não-root (UID
1001) por padrão — compatível de cara com a SCC `restricted-v2` do OpenShift.

```bash
# a partir da raiz do repositório
docker build -t omnicraft-server:ubi -f deploy/docker/Dockerfile.ubi .
```

Depois referencie a imagem no overlay do OpenShift, aplicando um patch no
Deployment ou apontando seu image stream para ela.

## Deploy no Red Hat OpenShift

O overlay `overlays/openshift/` substitui o Ingress por uma Route do
OpenShift (TLS de borda, gerenciado pela plataforma) e adiciona um
SecurityContext compatível com `restricted-v2`. Não precisa de controlador de
ingress nem dos add-ons do cert-manager.

1. **Edite o secret** em `base/secret.yaml` (igual ao caminho do banco de
   dados externo acima).

2. **Defina o hostname da sua route** — troque
   `omnicraft.apps.example.com` em `overlays/openshift/route.yaml` pelo
   domínio de apps do seu cluster.

3. **Aplique:**

   ```bash
   kubectl kustomize deploy/kubernetes/overlays/openshift/ | oc apply -f -
   ```

Para Postgres dentro do cluster no OpenShift, use `overlays/openshift-postgres/`
em vez disso — ele combina o StatefulSet do Postgres, a Route do OpenShift e os
security contexts restritos:

```bash
# edite overlays/openshift-postgres/secret-patch.yaml com senhas reais primeiro
kubectl kustomize deploy/kubernetes/overlays/openshift-postgres/ | oc apply -f -
```

## Runners de sandbox sob demanda

O overlay `overlays/sandbox-runners/` liga o provider de sandbox gerenciado
**`kubernetes`**: uma sessão `host_type: managed` gera um Pod runner que roda
`omnicraft host` como seu entrypoint e disca de volta pelo túnel do
launch-token. Ele adiciona um namespace dedicado para os runners, uma SA de
servidor com privilégio mínimo (direitos escopados de Pod + Secret, **sem
`pods/exec`**), e a config `sandbox:` do servidor. O overlay troca para a
variante oficial de imagem `omnicraft-server-kubernetes`, que adiciona o
extra `kubernetes` que o provider importa (a imagem base do servidor não o
inclui). Veja `overlays/sandbox-runners/README.md` para o guia completo.

```bash
kubectl apply -k deploy/kubernetes/overlays/sandbox-runners
# depois crie o Secret omnicraft-creds do harness (veja o README do overlay)
```

**Credenciais e autenticação** — duas preocupações separadas, não confunda:

- **Autenticação do servidor.** Coloque o servidor atrás de autenticação
  `header`/`oidc`, ou rode em modo single-user; o modo embutido `accounts`
  recusa o dial-back do runner por sessão (`403`), um limite a nível de
  framework compartilhado por todos os providers de sandbox — veja
  [Autenticação](../README.md#autenticação).
- **Chaves de modelo** (`ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` /
  `OPENAI_API_KEY` / `GIT_TOKEN` / …) viajam no Secret `omnicraft-creds`,
  projetado em todo Pod runner.

Os dois são detalhados em
[`overlays/sandbox-runners/README.md`](overlays/sandbox-runners/README.md#autenticação-do-servidor-hosts-gerenciados).

## Verifique o deploy

Confira o rollout e alcance o servidor sem um domínio público:

```bash
kubectl get pods -n omnicraft          # omnicraft (and, with the overlay, postgres) → Running
kubectl rollout status deploy/omnicraft -n omnicraft
kubectl logs -n omnicraft deploy/omnicraft          # server logs

# faça o port-forward do Service e abra o app localmente:
kubectl port-forward -n omnicraft svc/omnicraft 8000:80
# → http://localhost:8000   (health check: curl localhost:8000/health → {"status":"ok"})
```

O primeiro boot roda as migrações do banco de dados antes do servidor começar
a escutar; o pod pode reiniciar uma vez se a liveness probe disparar durante
essa janela (veja
[Dimensionamento de recursos](#dimensionamento-de-recursos)).

Para testar o próprio Ingress em vez de usar port-forward, aponte o hostname
dele para um domínio que já resolve para localhost — `omnicraft.localtest.me`
ou `<node-ip>.sslip.io` — use o issuer autoassinado acima, e alcance-o pela
porta publicada do controlador de ingress.

## Próximos passos: conecte um host

O servidor é o plano de controle — os agentes rodam em **hosts** que se
registram nele. Um deploy novinho não tem nenhum, então conecte pelo menos uma
máquina:

```bash
omnicraft login https://omnicraft.example.com          # authenticate the CLI
omnicraft host  --server https://omnicraft.example.com # register this machine
```

O host então aparece na web UI quando você inicia um chat novo. Veja o
[README principal](../../README.md) para a referência completa de host/auth.

## Use seu próprio IdP em vez disso (OIDC) — opcional

Opcional. O provider `accounts` padrão (usuário + senha) funciona de cara; use
isto só para delegar a autenticação a um provider OIDC externo. Adicione as
variáveis de ambiente do OIDC ao secret:

```bash
kubectl create secret generic omnicraft-oidc -n omnicraft \
  --from-literal=OMNICRAFT_AUTH_PROVIDER=oidc \
  --from-literal=OMNICRAFT_OIDC_ISSUER=https://github.com \
  --from-literal=OMNICRAFT_OIDC_CLIENT_ID=<client-id> \
  --from-literal=OMNICRAFT_OIDC_CLIENT_SECRET=<client-secret> \
  --from-literal=OMNICRAFT_OIDC_REDIRECT_URI=https://omnicraft.example.com/auth/callback \
  --from-literal=OMNICRAFT_OIDC_COOKIE_SECRET=$(openssl rand -hex 32)
```

Depois adicione `envFrom: [{secretRef: {name: omnicraft-oidc}}]` à spec do
container do Deployment (ou junte os valores em `omnicraft-secrets`).

## Dimensionamento de recursos

O servidor fica ocioso em torno de ~275 MB de RSS. Os manifests pedem 512 Mi e
limitam em 1 Gi — ajuste a gosto. O primeiro boot contra um Postgres remoto
roda migrações e leva ~1 minuto; aumente o `initialDelaySeconds` da liveness
para ~90s se você ver o pod ser morto durante o primeiro deploy.

## Escalonamento

O servidor usa um registro de runners em memória, então **só uma réplica é
suportada**. Não aumente `replicas` a menos que a arquitetura seja alterada
para usar um registro compartilhado (ex.: Redis).
