# OmniCraft — stack docker-compose

Rode o servidor como uma stack Docker autocontida em qualquer host: seu
notebook, uma VPS, uma instância EC2, um servidor de casa, em qualquer lugar
onde `docker compose` rode.

A stack:
- `postgres` — banco de dados persistente num volume Docker
- `omnicraft` — a imagem do servidor (construída a partir de `../Dockerfile`)

A autenticação é in-process — o servidor tem embutidos os modos header-proxy
e OIDC nativo (veja [Modo multiusuário](#modo-multiusuário-oidc) abaixo). Não
há um container separado de auth-proxy.

## Início rápido (usuário único)

```bash
cd deploy/docker
./bootstrap.sh                          # gera POSTGRES_PASSWORD + cookie secret no .env
docker compose up -d
docker compose logs -f omnicraft       # ctrl-c quando o boot estiver limpo
```

O `bootstrap.sh` é idempotente — rodá-lo de novo deixa os secrets já
definidos intactos. Se você preferir gerenciar o `.env` você mesmo, basta
`cp .env.example .env` e editar `POSTGRES_PASSWORD` (e
`OMNICRAFT_OIDC_COOKIE_SECRET` se você for ativar o OIDC) manualmente.

O servidor está em http://localhost:8000. A web UI imprime o comando de CLI
para lançar um runner local contra ele. Do seu notebook:

```bash
omnicraft run path/to/agent.yaml --server http://localhost:8000
```

Reinicie tudo do zero (apaga o banco de dados e o armazenamento de
artefatos):

```bash
docker compose down -v
```

## Modo multiusuário (accounts — padrão)

Autenticação `accounts` embutida: nenhum IdP para registrar, nenhum proxy
para hospedar. Este é o padrão — `docker compose up -d` já sobe com ela, sem
nenhuma variável de ambiente extra para configurar. O primeiro boot cria um
usuário admin (nomeado a partir do usuário do SO de quem operou, caindo para
`admin` em containers headless) com uma senha aleatória que aparece nos logs
do container e no volume persistente em `/data/admin-credentials`.

Para qualquer deploy alcançável por um domínio público, defina também a URL
externa para que os links de convite resolvam corretamente:

```bash
# Adicione ao .env (o bootstrap.sh já gerou o cookie secret para você):
OMNICRAFT_ACCOUNTS_BASE_URL=https://omnicraft.example.com

docker compose up -d
docker compose logs omnicraft | grep -A4 "Created initial admin"
```

Copie o `password` aleatório da linha de log para o formulário de login da
web UI, depois:

- Clique no seu nome de usuário no canto superior direito → **Members** →
  **Invite member**.
- Compartilhe a URL de uso único com o colega; ele escolhe o próprio nome de
  usuário e senha ao resgatá-la.
- O sign-out fica no mesmo menu de conta.

Deploy headless (CI, Cloud Run, etc.) onde você não consegue ler os logs?
Pré-defina a senha:

```bash
OMNICRAFT_ACCOUNTS_INIT_ADMIN_PASSWORD=<your-strong-password>
```

O arquivo persistente da senha fica em `/data/admin-credentials` no volume
`artifact-data` — sobrevive a `docker compose restart`, e é apagado por
`docker compose down -v`.

## Modo multiusuário (OIDC)

O modo usuário único confia em qualquer um que alcance a porta e usa a
identidade `"local"` para todas as requisições. Para um deploy compartilhado,
o servidor tem suporte nativo a OIDC — ele mesmo cuida do fluxo de login
inteiro (`/auth/login`, `/auth/callback`, `/auth/logout`) com um cookie de
sessão assinado. Sem container extra, sem shim de basic-auth do Caddy, sem
oauth2-proxy.

### Passo a passo: GitHub OAuth (o mais fácil de registrar)

1. **Registre o app OAuth.** Vá a https://github.com/settings/developers →
   New OAuth App. Defina o callback como `https://<your-host>/auth/callback`
   (HTTPS é fortemente recomendado; o GitHub permite HTTP para testes, mas
   avisa).

2. **Gere um cookie secret.** O `./bootstrap.sh` já fez isso no caminho do
   início rápido — `OMNICRAFT_OIDC_COOKIE_SECRET` está definido no seu
   `.env`. Se você pulou essa etapa, rode `openssl rand -hex 32` e cole o
   valor você mesmo.

3. **Edite o `.env`:**
   ```bash
   OMNICRAFT_AUTH_PROVIDER=oidc
   OMNICRAFT_OIDC_ISSUER=https://github.com
   OMNICRAFT_OIDC_CLIENT_ID=Iv1.abc123…
   OMNICRAFT_OIDC_CLIENT_SECRET=…
   OMNICRAFT_OIDC_REDIRECT_URI=https://omnicraft.example.com/auth/callback
   # OMNICRAFT_OIDC_COOKIE_SECRET já está definido pelo bootstrap.sh — não mexa.
   ```

4. **Suba tudo.**
   ```bash
   docker compose up -d
   ```

   O servidor vai falhar ruidosamente na inicialização se alguma variável de
   ambiente do OIDC obrigatória estiver faltando — confira
   `docker compose logs omnicraft` se ele não subir.

5. **Visite a URL** → você deve ser redirecionado ao GitHub para entrar, e
   então de volta para a web UI com um cookie `__Host-ap_session` definido.

### Passo a passo: Google Workspace (com allowlist de domínio)

```bash
OMNICRAFT_AUTH_PROVIDER=oidc
OMNICRAFT_OIDC_ISSUER=https://accounts.google.com
OMNICRAFT_OIDC_CLIENT_ID=…apps.googleusercontent.com
OMNICRAFT_OIDC_CLIENT_SECRET=…
OMNICRAFT_OIDC_REDIRECT_URI=https://omnicraft.example.com/auth/callback
OMNICRAFT_OIDC_COOKIE_SECRET=<64-hex-chars>
OMNICRAFT_OIDC_ALLOWED_DOMAINS=example.com,subsidiary.example.com
```

`ALLOWED_DOMAINS` é crítico quando a tela de consentimento OAuth é
"External" — sem ele, qualquer conta do Google no planeta consegue entrar.

### OIDC genérico (Okta, Auth0, Keycloak, Entra ID)

Qualquer IdP que publique `/.well-known/openid-configuration` funciona.
Defina `OMNICRAFT_OIDC_ISSUER` para a URL base; o servidor busca o discovery
na inicialização.

```bash
OMNICRAFT_AUTH_PROVIDER=oidc
OMNICRAFT_OIDC_ISSUER=https://your-tenant.okta.com
OMNICRAFT_OIDC_CLIENT_ID=…
OMNICRAFT_OIDC_CLIENT_SECRET=…
OMNICRAFT_OIDC_REDIRECT_URI=https://omnicraft.example.com/auth/callback
OMNICRAFT_OIDC_COOKIE_SECRET=<64-hex-chars>
```

### HTTPS para a URL de callback

A maioria dos IdPs exige HTTPS para redirect URIs que não sejam localhost, e
o cookie de sessão usa o prefixo `__Host-`, que os navegadores só aceitam
sobre HTTPS. Três opções:

1. **Use o overlay do Caddy incluído** (o mais fácil — qualquer VPS / EC2 /
   servidor de casa com um domínio público):

   ```bash
   # No .env:
   OMNICRAFT_DOMAIN=omnicraft.example.com
   OMNICRAFT_ACME_EMAIL=you@example.com      # opcional, para avisos do Let's Encrypt

   # Aponte os registros DNS A/AAAA para o host, depois:
   docker compose -f docker-compose.yaml -f docker-compose.https.yaml up -d
   ```

   O Caddy provisiona e renova automaticamente um certificado Let's Encrypt;
   o container omnicraft para de ser exposto diretamente e só as portas :80
   e :443 são publicadas. Exige o Docker Compose 2.24+ para a diretiva
   `!reset` do overlay. Veja o `Caddyfile` para a configuração (de 3 linhas).

2. **Atrás de um reverse proxy existente** — aponte seu proxy para
   `omnicraft:8000` pela rede docker (ou `127.0.0.1:8000` a partir do host).
   Exemplos: AWS ALB com certificado ACM, Cloudflare no modo SSL "Full",
   certificados de plataforma da Fly.io / Cloud Run / Render.

## Modo header-proxy (para deploys atrás de um proxy SSO existente)

Se você já tem oauth2-proxy, Databricks Apps, AWS ALB OIDC, Cloudflare
Access, Tailscale Funnel, ou qualquer outro proxy que injete um header de
identidade, defina `OMNICRAFT_AUTH_PROVIDER=header`. O servidor vai rejeitar
requisições sem o header.

```bash
OMNICRAFT_AUTH_PROVIDER=header
```

O header lido é `X-Forwarded-Email` por padrão. Proxies que usam um nome de
header diferente definem `OMNICRAFT_AUTH_HEADER` para apontar o servidor
para ele — por exemplo, o Cloudflare Access fornece o email autenticado em
`Cf-Access-Authenticated-User-Email`:

```bash
OMNICRAFT_AUTH_PROVIDER=header
OMNICRAFT_AUTH_HEADER=Cf-Access-Authenticated-User-Email
```

Alguns proxies colocam um prefixo no valor que injetam. O Google IAP
encaminha o email em `X-Goog-Authenticated-User-Email` prefixado com
`accounts.google.com:`; defina `OMNICRAFT_AUTH_HEADER_STRIP_PREFIX` para
remover esse prefixo e recuperar o email puro:

```bash
OMNICRAFT_AUTH_PROVIDER=header
OMNICRAFT_AUTH_HEADER=X-Goog-Authenticated-User-Email
OMNICRAFT_AUTH_HEADER_STRIP_PREFIX=accounts.google.com:
```

**Nota de segurança:** neste modo, o proxy é responsável por remover
qualquer cópia do header de identidade vinda da requisição do cliente —
caso contrário, qualquer visitante pode forjar uma identidade. O servidor
confia em qualquer valor que chegue até ele.

## Variáveis de ambiente

| Variável | Padrão | Finalidade |
|---|---|---|
| `POSTGRES_PASSWORD` | *obrigatório* | Senha do banco para o container Postgres incluído. |
| `POSTGRES_USER` / `POSTGRES_DB` | `omnicraft` | Usuário do banco + nome do banco de dados. |
| `OMNICRAFT_PORT` | `8000` | Porta do host em que o servidor é publicado. |
| `OMNICRAFT_AUTH_ENABLED` | `1` (no compose) | Chave mestra de autenticação. `1` → accounts (ou oidc se `OMNICRAFT_OIDC_ISSUER` estiver definido); `0` → modo local de usuário único (toda requisição é o usuário compartilhado `local` — só para dev local, nunca para deploys compartilhados). |
| `OMNICRAFT_AUTH_PROVIDER` | não definido | Saída de emergência para fixar um modo explicitamente: `header` / `accounts` / `oidc`. Sobrescreve a seleção automática do `AUTH_ENABLED`. |
| `OMNICRAFT_AUTH_HEADER` | `X-Forwarded-Email` | Só no modo header: nome do header de identidade confiável. Defina para proxies que usam outro nome, ex.: `Cf-Access-Authenticated-User-Email` (Cloudflare Access). |
| `OMNICRAFT_AUTH_HEADER_STRIP_PREFIX` | não definido (não remove nada) | Só no modo header: prefixo removido do valor do header de identidade. Defina como `accounts.google.com:` para o `X-Goog-Authenticated-User-Email` do Google IAP. |
| `OMNICRAFT_OIDC_*` | não definido | Configuração OIDC — obrigatória no modo oidc (issuer definido, ou `AUTH_PROVIDER=oidc`). Veja `.env.example`. |
| `PYPI_INDEX_URL` | `https://pypi.org/simple` | Índice do PyPI usado no build — sobrescreva só atrás de um proxy corporativo. |

`DATABASE_URL` e `ARTIFACT_DIR` são calculados pelo compose e injetados no
container.

## Imagem do host (`--target host`)

O mesmo Dockerfile publica uma segunda imagem: a imagem oficial de **host**
do OmniCraft, da qual sandboxes remotos sobem para começar em segundos em
vez de pagar o custo de uma instalação de dependências dentro do sandbox.
Ela traz a instalação completa do omnicraft (os três pacotes + dependências,
`python` e `pip` no PATH), `git` (workspaces / worktrees), `tmux` (sessões de
terminal lançadas por harnesses nativos), e as CLIs de harness de código —
`claude`, `codex`, `pi`, e `kiro-cli`, com o runtime que precisam — então
agentes claude-sdk / claude-native / codex / pi / kiro-native rodam em
sandboxes sem uma instalação dentro do sandbox. Nenhuma parte exclusiva do
servidor é incluída (sem bundle SPA, sem psycopg, sem entrypoint do uvicorn).

A CI publica essa imagem ao lado da imagem do servidor, com o mesmo esquema
de tags:

- `ghcr.io/omnicraft-ai/omnicraft-host:latest` — acompanha o HEAD da main
  (o padrão para `omnicraft sandbox create --provider modal`)
- `ghcr.io/omnicraft-ai/omnicraft-host:sha-<short>` — fixação imutável por
  commit
- `ghcr.io/omnicraft-ai/omnicraft-host:vX.Y.Z` — tags de release

Construa-a localmente a partir da raiz do repositório:

```bash
docker build -t omnicraft-host:latest --target host \
             -f deploy/docker/Dockerfile .
```

### Usando-a com o provedor de sandbox Modal

`omnicraft sandbox create --provider modal` sobe sandboxes a partir de
`ghcr.io/omnicraft-ai/omnicraft-host:latest` por padrão. Os wheels do seu
checkout local ainda são construídos e sobrepostos por cima no momento da
criação (`pip install --force-reinstall --no-deps`), então o sandbox roda
exatamente o seu código — a imagem pré-assada só fornece a árvore de
dependências. Um checkout que adiciona uma dependência totalmente nova
precisa que esse pacote seja instalado manualmente no sandbox até a imagem
oficial ser reconstruída com ele.

Duas variáveis de ambiente ajustam o pull:

| Variável | Finalidade |
|---|---|
| `OMNICRAFT_MODAL_HOST_IMAGE` | Sobrescreve a referência da imagem, ex.: uma cópia interna da organização (`ghcr.io/<your-org>/omnicraft-host:latest`) ou uma fixação `:sha-<short>`. |
| `OMNICRAFT_MODAL_REGISTRY_SECRET` | Nome de um [secret da Modal](https://modal.com/secrets) guardando credenciais de registry para pulls privados. Crie-o com as chaves `REGISTRY_USERNAME` (seu usuário de registry) e `REGISTRY_PASSWORD` (para o GHCR: um personal access token com `read:packages`). Não definido = pull anônimo. |

### Usando-a com o provedor de sandbox Daytona

A mesma imagem de host dá suporte às sessões gerenciadas pela Daytona
(configuração do servidor `sandbox.provider: daytona`; a Daytona é
managed-only — não há um fluxo de CLI `omnicraft sandbox create --provider
daytona`). A Daytona ingere a imagem do registry num snapshot interno no
primeiro uso (o primeiro lançamento a partir de uma dada imagem leva
minutos; lançamentos posteriores reaproveitam o snapshot e levam segundos).
Sobrescreva a referência com `OMNICRAFT_DAYTONA_HOST_IMAGE` ou o
`sandbox.daytona.image` da configuração do servidor. Veja
[`deploy/daytona/README.md`](../daytona/README.md) para o guia completo do
provedor (credenciais, o relay de saída do nível gratuito, e considerações
de segurança).

## Documentos de design relacionados

- `designs/OIDC_AUTH.md` — design completo do OIDC nativo
- `designs/SESSIONS_AUTH.md` — contrato `AuthProvider` + sistema de
  permissões
