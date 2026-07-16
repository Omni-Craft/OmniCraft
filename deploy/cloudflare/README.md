# OmniCraft na Cloudflare (Containers + D1 + R2)

Rode o servidor OmniCraft em **Cloudflare Containers**, com **D1** como banco
de dados e **R2** como o armazenamento durável de artefatos. Essa é a opção
serverless, com escala a zero: sem VM nem Postgres para administrar, uma URL
pública `*.workers.dev` (ou o seu domínio), e o container dorme quando ocioso.

> [!NOTE]
> Isso **não** é a mesma coisa que [`deploy/trycloudflare/`](../trycloudflare/),
> que é um túnel rápido que expõe um servidor rodando no **seu notebook**.
> Aqui o servidor em si roda **na Cloudflare**.

> [!NOTE]
> Esse caminho usa um pequeno shim de dialeto SQLAlchemy (`sitecustomize.py`)
> porque o Cloudflare D1 ainda não é de primeira classe no OmniCraft. Ele
> funciona de ponta a ponta — foi assim que este diretório foi validado — e as
> migrações normais de boot rodam sem modificação. O armazenamento de
> artefatos do R2, por outro lado, já usa um backend de primeira classe
> (`S3ArtifactStore`) adicionado junto com este diretório.

## Como funciona

```
        HTTPS / WebSocket
browser ───────────────►  Worker (src/index.js)
                              │   getContainer("singleton").fetch(req)
                              ▼
                          Container  ──►  o servidor omnicraft (porta 8000)
                          (1 instância)       │            │
                          DATABASE_URL ───────┘            │  API S3 (boto3)
                          cloudflare_d1://…                ▼
                                 │                  OMNICRAFT_ARTIFACT_URI
                                 ▼                  s3://omnicraft-artifacts
                          Cloudflare D1                    │
                          (SQLite, o banco)                ▼
                                                    Cloudflare R2
                                                    (armazenamento de artefatos)
```

- **Worker** — uma frente fina que repassa cada requisição para **uma**
  instância de container (o OmniCraft mantém um registro de runners em
  memória, então é single-replica).
- **Container** — a imagem oficial `ghcr.io/omnicraft-ai/omnicraft-server`
  mais o dialeto SQLAlchemy do D1, um shim que o re-registra como um dialeto
  SQLite de verdade, e o `boto3` (o `Dockerfile` deste diretório).
- **D1** é o banco de dados. O servidor o alcança pelo dialeto
  `sqlalchemy-cloudflare-d1`, que fala a API HTTP do D1 — então a
  `DATABASE_URL` é `cloudflare_d1://<account>:<api-token>@<database-id>`.
- **R2** é o armazenamento de artefatos. O disco do container da Cloudflare é
  **efêmero**, então os artefatos (bundles de agente, arquivos do usuário) vão
  para o R2 pela sua **API S3**, via `S3ArtifactStore` nativo do OmniCraft,
  selecionado com `OMNICRAFT_ARTIFACT_URI=s3://<bucket>`. Sem montagem FUSE,
  sem sidecar.

## O que tem aqui

| Arquivo | Finalidade |
|---|---|
| `Dockerfile` | imagem derivada: servidor + dialeto D1 + shim + boto3 |
| `sitecustomize.py` | shim que re-registra `cloudflare_d1` como um dialeto SQLite (carregado automaticamente) |
| `src/index.js` | o Worker que repassa para o container |
| `wrangler.jsonc` | configuração do Worker + Container + Durable Object |
| `package.json` | `wrangler` + `@cloudflare/containers` |

## Pré-requisitos

- Uma conta Cloudflare no plano **Workers Paid** — Containers exige isso.
- **Docker** rodando localmente (`wrangler deploy` constrói a imagem).
- **Node** (para o `wrangler`).
- `wrangler login` (ou um `CLOUDFLARE_API_TOKEN`).

```bash
cd deploy/cloudflare
npm install
npx wrangler login
```

## Deploy

### 1. Crie o banco D1

```bash
npx wrangler d1 create omnicraft
# anote o "database_id" que ele imprime — chame de <DATABASE_ID>
```

### 2. Crie o bucket R2

```bash
npx wrangler r2 bucket create omnicraft-artifacts
```

### 3. Um token de API do D1 (para a `DATABASE_URL`)

O dialeto se autentica na API REST do D1 com um **token de API** da
Cloudflare. Crie um em **dash.cloudflare.com → My Profile → API Tokens →
Create Token → Custom**, com a permissão **Account → D1 → Edit**. Sua
`DATABASE_URL` fica então:

```
cloudflare_d1://<ACCOUNT_ID>:<D1_API_TOKEN>@<DATABASE_ID>
```

### 4. Credenciais S3 do R2 (para o armazenamento de artefatos)

O armazenamento de artefatos usa a **API S3** do R2, que precisa de um Access
Key ID + Secret Access Key. Crie-os em **dash.cloudflare.com → R2 → Manage R2
API Tokens → Create API Token → Object Read & Write**. Ele mostra um **Access
Key ID** e uma **Secret Access Key** uma única vez — guarde os dois.

<details>
<summary>Alternativa: derivar as chaves S3 de um token de API existente</summary>

Qualquer token de API com permissões de R2 pode ser usado como credencial S3
sem gerar um token de R2 separado
([documentação](https://developers.cloudflare.com/r2/api/tokens/)):
**Access Key ID** = o *id* do token, **Secret Access Key** =
`sha256(valor do token)`.

```bash
python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "<TOKEN_VALUE>"
```
</details>

### 5. Configure e defina os secrets

No `wrangler.jsonc`, defina `AWS_ENDPOINT_URL_S3` para o endpoint de R2 da
sua conta (`https://<ACCOUNT_ID>.r2.cloudflarestorage.com`). Depois defina os
quatro secrets:

```bash
# DATABASE_URL — a string cloudflare_d1:// do passo 3
npx wrangler secret put DATABASE_URL

# Secret do cookie de sessão — qualquer string hex de 64 caracteres
openssl rand -hex 32 | npx wrangler secret put OMNICRAFT_ACCOUNTS_COOKIE_SECRET

# Credenciais S3 do R2, do passo 4
npx wrangler secret put AWS_ACCESS_KEY_ID
npx wrangler secret put AWS_SECRET_ACCESS_KEY
```

### 6. Deploy

```bash
npx wrangler deploy
# -> https://omnicraft.<your-subdomain>.workers.dev
```

O container faz cold-start na primeira requisição (~10s), e depois se mantém
aquecido:

```bash
curl https://omnicraft.<your-subdomain>.workers.dev/health   # {"status":"ok"}
```

Num D1 novinho em folha, o **primeiro** boot roda todas as migrações antes do
servidor começar a escutar (~1 minuto contra a API REST do D1), então as
primeiras requisições podem devolver um 5xx enquanto ele migra — é só tentar
de novo. Os boots seguintes são rápidos.

### 7. Primeiro admin + conectar um host

Abra a URL e a tela de Setup reivindica o primeiro admin (usuário + senha).
Depois conecte uma máquina para de fato rodar agentes (o servidor é só o
plano de controle):

```bash
omnicraft login https://omnicraft.<your-subdomain>.workers.dev
omnicraft host  --server https://omnicraft.<your-subdomain>.workers.dev
```

## Verificando a durabilidade

O ponto do R2 é que o estado sobrevive ao container efêmero. Para provar
isso, anote seus dados, force um container novo (`npx wrangler deploy` de
novo, ou deixe ele dormir sozinho), e confirme que os dados continuam lá —
agentes ainda carregam, sessões ainda existem. O banco vive no D1 e os
artefatos no R2; o container não guarda nada durável.
