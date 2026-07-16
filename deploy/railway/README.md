# OmniCraft na Railway

Publique o OmniCraft na Railway. A Railway puxa a imagem pré-construída, roda
ela ao lado de um Postgres gerenciado, e serve tudo por HTTPS em
`*.up.railway.app`.

> **A Railway ainda não é um clique só de verdade.** Diferente do `render.yaml`
> da Render (totalmente declarativo — Postgres, porta e env todos conectados
> automaticamente), um `railway.toml` puro deixa várias coisas para conectar na
> mão (passos abaixo). Uma experiência de um clique de verdade precisa de um
> **template publicado da Railway** que já venha com a referência do Postgres,
> o `HOST` e a porta de destino pré-conectados — isso está rastreado como um
> follow-up. Até lá, use os passos manuais aqui. (A Render é o caminho mais
> tranquilo hoje.)

<!-- TODO(oss-release): publicar um template da Railway (pré-conectando Postgres + HOST +
     porta) e adicionar o botão:
     [![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/<template-id>) -->

## O que é provisionado

- **omnicraft** — serviço web que puxa `ghcr.io/omnicraft-ai/omnicraft-server`
  via `deploy/docker/Dockerfile.prebuilt`, servido em
  `https://<project>.up.railway.app`.
- **Postgres** — plugin PostgreSQL gerenciado pela Railway, que você adiciona
  ao projeto. A Railway conecta o `DATABASE_URL` dele no app como uma
  referência à variável da instância do banco (praticamente automático), mas o
  valor pode demorar a propagar no primeiro deploy — veja o passo 2.

O armazenamento de artefatos usa o sistema de arquivos local do container por
padrão (efêmero entre redeploys). Para persistência, adicione um Volume da
Railway montado em `/data/artifacts`.

> **Opcional: Postgres externo da Neon.** Em vez do plugin da Railway, você
> pode apontar o `DATABASE_URL` para um banco da Neon
> ([pg.new](https://pg.new)) — por exemplo, para o scale-to-zero serverless da
> Neon ou para branching. O tradeoff: você perde o provisionamento integrado
> (um cadastro separado + string de conexão) e ganha alguma latência
> entre provedores, então o plugin da Railway continua sendo o padrão mais
> simples.

## Configuração (contas embutidas — o padrão)

Usa por padrão o provedor de autenticação `accounts`: multiusuário, sem IdP
externo. Os passos abaixo foram validados ponta a ponta:

1. **Publique a partir do repositório** — New Project → Deploy from GitHub
   repo → este repositório. A Railway lê o `railway.toml` e puxa a imagem.
   **Adicione um plugin de Postgres** ao projeto.
2. **Banco de dados** — A Railway conecta o `DATABASE_URL` do Postgres no app
   como uma referência à variável da instância do banco (praticamente
   automático ao adicionar o plugin). Se o primeiro deploy der erro com
   `DATABASE_URL is required`, o valor da referência simplesmente ainda não
   tinha propagado — **faça o redeploy** e resolve. (Para confirmar, o serviço
   do app deve ter uma variável `DATABASE_URL` referenciando o serviço do
   Postgres, ex.: `${{Postgres.DATABASE_URL}}`.)
3. **Pegue a senha do admin** nos **Deploy logs** do primeiro boot (impressa
   uma vez; idempotente — boots seguintes não reimprimem):
   ```
   ✓ Created initial admin account (accounts auth provider).
       password: <generated>
   ```
   Ela também é gravada em `/data/admin-credentials`.
4. Abra a URL, entre como `admin`, convide colegas em **Members**.

> **O `HOST` é tratado automaticamente.** A Railway injeta `HOST=[::]`, que um
> bind de socket não consegue usar e que o edge IPv4 da Railway não consegue
> alcançar; o entrypoint detecta a Railway e converte para `0.0.0.0`, então
> nenhuma variável `HOST` manual é necessária. Se o domínio gerado retornar
> "Application failed to respond", o auto-detect de porta da Railway escolheu
> a porta errada — abra Settings → Networking e defina a porta de destino do
> domínio para a `PORT` que a Railway injetou (mostrada no log de boot como
> `Uvicorn running on …:<port>`).

> O segredo do cookie é gerado automaticamente e o
> `OMNICRAFT_ACCOUNTS_BASE_URL` é auto-detectado a partir de
> `RAILWAY_PUBLIC_DOMAIN`, então essas duas variáveis não precisam ser
> definidas. Para fixar uma senha de admin conhecida, defina
> `OMNICRAFT_ACCOUNTS_INIT_ADMIN_PASSWORD` antes do primeiro boot.

## Use o seu próprio IdP (OIDC)

Prefere login via GitHub / Google / Okta em vez das contas embutidas? Troque o
provedor nas Variables do serviço. O OIDC exige HTTPS — a Railway fornece isso
automaticamente em `*.up.railway.app`. Se você definir um domínio
personalizado, aponte-o para o seu projeto antes de concluir estes passos.

### GitHub OAuth (o mais simples de cadastrar)

1. Vá para `github.com/settings/developers` → **New OAuth App**.
   - Homepage URL: `https://<project>.up.railway.app`
   - Authorization callback URL: `https://<project>.up.railway.app/auth/callback`
   - Clique em **Register application**, depois em **Generate a new client
     secret**.

2. No seu projeto Railway, abra o serviço **omnicraft** → **Variables** e
   adicione:

   | Variable | Value |
   |---|---|
   | `OMNICRAFT_AUTH_PROVIDER` | `oidc` |
   | `OMNICRAFT_OIDC_ISSUER` | `https://github.com` |
   | `OMNICRAFT_OIDC_CLIENT_ID` | seu client ID do GitHub OAuth |
   | `OMNICRAFT_OIDC_CLIENT_SECRET` | seu client secret do GitHub OAuth |
   | `OMNICRAFT_OIDC_REDIRECT_URI` | `https://<project>.up.railway.app/auth/callback` |
   | `OMNICRAFT_OIDC_COOKIE_SECRET` | saída de `openssl rand -hex 32` |

3. A Railway faz redeploy automaticamente. Visite a URL — você será
   redirecionado para o GitHub para entrar.

### Google Workspace

| Variable | Value |
|---|---|
| `OMNICRAFT_AUTH_PROVIDER` | `oidc` |
| `OMNICRAFT_OIDC_ISSUER` | `https://accounts.google.com` |
| `OMNICRAFT_OIDC_CLIENT_ID` | `…apps.googleusercontent.com` |
| `OMNICRAFT_OIDC_CLIENT_SECRET` | seu client secret |
| `OMNICRAFT_OIDC_REDIRECT_URI` | `https://<project>.up.railway.app/auth/callback` |
| `OMNICRAFT_OIDC_COOKIE_SECRET` | saída de `openssl rand -hex 32` |
| `OMNICRAFT_OIDC_ALLOWED_DOMAINS` | `example.com` (crítico — veja a nota abaixo) |

> **Importante:** Sem `OMNICRAFT_OIDC_ALLOWED_DOMAINS`, qualquer conta Google
> consegue entrar quando a tela de consentimento OAuth é "External". Sempre
> restrinja ao seu domínio.

### OIDC genérico (Okta, Auth0, Keycloak, Entra ID)

Defina `OMNICRAFT_OIDC_ISSUER` para a URL base do seu IdP (a que publica
`/.well-known/openid-configuration`). O resto das variáveis é o mesmo de
acima.

## Domínio personalizado

No seu projeto Railway, abra **Settings** → **Domains** → **Add domain**.
Aponte seu registro DNS A/AAAA para o endereço atribuído pela Railway. A
Railway provisiona um certificado Let's Encrypt automaticamente.

Atualize `OMNICRAFT_OIDC_REDIRECT_URI` para usar o domínio personalizado
depois que o DNS propagar.

## Atualizando

A Railway faz redeploy automaticamente quando uma tag de imagem nova é
publicada no GHCR (se você configurou um webhook) ou sob demanda:

1. No painel da Railway, abra o serviço **omnicraft**.
2. Clique em **Deploy** → **Latest** para puxar a imagem `:latest` mais nova.

## Custo

Plano Railway Hobby: ~$5/mês de base + uso de CPU/memória por minuto. Uma
instância do OmniCraft levemente carregada (poucos usuários simultâneos)
costuma ficar abaixo de $10–15/mês no total, incluindo o plugin de Postgres.

## Publicando o template

Configuração única, feita pelo dono do repositório depois que o repositório
fica público:

1. Vá para `railway.com/new/template` e clique em **Create template**.
2. Aponte para `github.com/omnicraft-ai/omnicraft`.
3. Selecione o plugin **Postgres**.
4. Preencha as env vars padrão com descrições para os campos opcionais de
   OIDC.
5. Clique em **Publish**. Copie a URL de deploy gerada e atualize o badge no
   topo deste arquivo e em `deploy/README.md`.
