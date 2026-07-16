# OmniCraft na Render

Publique o OmniCraft na Render em um clique. A Render provisiona o app e um
banco Postgres gerenciado, atribui uma URL HTTPS em `*.onrender.com`, e cuida
do SSL automaticamente. Nenhuma ferramenta local necessária.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/omnicraft-ai/omnicraft)

> **Nota:** O botão aponta para o repositório público
> `github.com/omnicraft-ai/omnicraft`. Ele entra no ar assim que esse
> repositório **e** o pacote `ghcr.io/omnicraft-ai/omnicraft-server` ficarem
> públicos; até lá, só funciona se você conectar a Render ao repositório
> (privado) pelo painel primeiro.

## O que é provisionado

O blueprint `render.yaml` na raiz do repositório define:

- **omnicraft** (serviço web Starter) — puxa a imagem pré-construída
  `ghcr.io/omnicraft-ai/omnicraft-server:latest` (construída pela CI; já traz o
  bundle da web UI), servida em `https://omnicraft-<hash>.onrender.com`.
  Enquanto o pacote GHCR for privado, adicione uma credencial de registry da
  Render e referencie-a no `render.yaml` (`image.creds`); assim que ficar
  público, o pull passa a ser anônimo.
- **omnicraft-db** (Postgres gerenciado `basic-256mb`) — o `DATABASE_URL` é
  injetado no serviço automaticamente.
- **artifact-data** (disco persistente de 10 GB) — montado em `/data`, para
  que a configuração do servidor, as credenciais do primeiro boot, os
  segredos de cookie e os artefatos dos agentes sobrevivam aos redeploys. Os
  artefatos ficam em `/data/artifacts`.

## Início rápido (contas embutidas — o padrão)

O blueprint usa por padrão o provedor de autenticação embutido `accounts`:
multiusuário logo de cara, sem IdP externo, e **nenhuma env var para
preencher** — o servidor gera o próprio segredo de cookie e auto-detecta a sua
URL pública a partir da Render.

1. Clique no botão Deploy to Render acima → **Apply**. Aguarde ~3–5 min para o
   pull da imagem + o health check.
2. **Pegue a senha do admin:** abra o serviço → **Logs** e ache o bloco do
   primeiro boot:
   ```
   ✓ Created initial admin account (accounts auth provider).
       password: <generated>
   ```
   (também gravada em `/data/admin-credentials` no disco; impressa uma vez).
3. Abra a sua URL `https://<service>.onrender.com`, entre como o admin, e
   convide colegas em **Members** na web UI.

> Para definir uma senha de admin conhecida em vez da gerada, adicione
> `OMNICRAFT_ACCOUNTS_INIT_ADMIN_PASSWORD` no painel antes do primeiro boot.

## Use o seu próprio IdP (OIDC)

Prefere delegar o login para GitHub / Google / Okta em vez das contas
embutidas? Troque o provedor depois do deploy inicial. O HTTPS já é fornecido
automaticamente pela Render.

### GitHub OAuth (o mais simples de cadastrar)

1. Vá para `github.com/settings/developers` → **New OAuth App**.
   - Homepage URL: `https://omnicraft-<hash>.onrender.com`
   - Authorization callback URL:
     `https://omnicraft-<hash>.onrender.com/auth/callback`
   - Clique em **Register application**, depois em **Generate a new client
     secret**.

2. No painel da Render, abra o serviço **omnicraft** → **Environment** e
   adicione / atualize estas variáveis:

   | Variable | Value |
   |---|---|
   | `OMNICRAFT_AUTH_PROVIDER` | `oidc` |
   | `OMNICRAFT_OIDC_ISSUER` | `https://github.com` |
   | `OMNICRAFT_OIDC_CLIENT_ID` | seu client ID do GitHub OAuth |
   | `OMNICRAFT_OIDC_CLIENT_SECRET` | seu client secret do GitHub OAuth |
   | `OMNICRAFT_OIDC_REDIRECT_URI` | `https://omnicraft-<hash>.onrender.com/auth/callback` |

   Adicione também `OMNICRAFT_OIDC_COOKIE_SECRET` = um valor de 64 caracteres
   hex de `openssl rand -hex 32` — o modo OIDC exige isso e valida como hex.

3. Clique em **Save Changes**. A Render faz redeploy automaticamente. Visite a
   URL — você será redirecionado para o GitHub para entrar.

### Google Workspace

| Variable | Value |
|---|---|
| `OMNICRAFT_AUTH_PROVIDER` | `oidc` |
| `OMNICRAFT_OIDC_ISSUER` | `https://accounts.google.com` |
| `OMNICRAFT_OIDC_CLIENT_ID` | `…apps.googleusercontent.com` |
| `OMNICRAFT_OIDC_CLIENT_SECRET` | seu client secret |
| `OMNICRAFT_OIDC_REDIRECT_URI` | `https://omnicraft-<hash>.onrender.com/auth/callback` |
| `OMNICRAFT_OIDC_ALLOWED_DOMAINS` | `example.com` (crítico — veja a nota abaixo) |

> **Importante:** Sem `OMNICRAFT_OIDC_ALLOWED_DOMAINS`, qualquer conta Google
> consegue entrar quando a tela de consentimento OAuth é "External". Sempre
> restrinja ao seu domínio.

### OIDC genérico (Okta, Auth0, Keycloak, Entra ID)

Defina `OMNICRAFT_OIDC_ISSUER` para a URL base do seu IdP (a que publica
`/.well-known/openid-configuration`). O resto das variáveis é o mesmo de
acima.

## Domínio personalizado

No painel da Render, abra o serviço **omnicraft** → **Settings** →
**Custom Domains** → **Add Custom Domain**. Aponte seu registro DNS CNAME
para o endereço atribuído pela Render. A Render provisiona um certificado
Let's Encrypt automaticamente.

Atualize `OMNICRAFT_OIDC_REDIRECT_URI` para usar o domínio personalizado
depois que o DNS propagar.

## Atualizando

A Render faz redeploy automaticamente quando um commit novo chega na branch
conectada (se o auto-deploy estiver ativado), ou manualmente:

1. No painel da Render, abra o serviço **omnicraft**.
2. Clique em **Manual Deploy** → **Deploy latest commit**.

## Custo

Render: ~$7/mês pelo serviço web Starter + ~$6/mês pelo Postgres gerenciado
`basic-256mb`. Total ~$13/mês para uma instância levemente carregada. Suba o
plano do Postgres (`basic-1gb`, …) para mais armazenamento.

> **Nota:** o serviço web precisa de uma instância paga (Starter+) por causa
> do disco persistente de artefatos, e os planos gratuitos de Postgres da
> Render expiram — então um tier pago de banco (`basic-256mb`) é o padrão
> persistente aqui.

> **Memória:** o serviço web Starter (512 MB) cobre o conjunto de trabalho de
> ~512 MB–1 GB do servidor. Não fique abaixo disso.

## Mais barato: SQLite no disco (nível leve)

Para um deploy de instância única você pode pular o Postgres gerenciado por
completo e rodar em **SQLite no disco persistente** — ele sobrevive a
redeploys (o disco sobrevive) e economiza o custo de ~$6/mês do banco. O
SQLite é um backend de primeira classe; o tradeoff é ficar limitado a uma
única instância (sem escala horizontal) e sem backups gerenciados, então
mantenha o Postgres para produção / múltiplas instâncias.

Para usar, remova o bloco `databases:` do `render.yaml` e troque a env var
`DATABASE_URL` por um caminho no disco:

```yaml
      - key: DATABASE_URL
        value: sqlite:////data/artifacts/chat.db
```

> **Ou um Postgres externo da Neon.** Você pode apontar o `DATABASE_URL` para
> um banco da Neon ([pg.new](https://pg.new)) em vez do gerenciado pela
> Render — por exemplo, para usar o tier *persistente* gratuito da Neon em vez
> do banco pago da Render. O tradeoff: você perde o auto-provisionamento
> integrado (um cadastro separado + string de conexão) e ganha alguma latência
> entre provedores, então o Postgres gerenciado da Render continua sendo o
> padrão mais simples.
