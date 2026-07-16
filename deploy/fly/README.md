# OmniCraft na Fly.io

Publique o OmniCraft na Fly.io. A Fly baixa a imagem pré-construída, roda-a ao
lado de um volume persistente, e a serve por HTTPS em `*.fly.dev`.

> **A Fly é CLI-first.** Não há um botão de um clique embutível como o da
> Render; você publica com `fly deploy` (ou, com um ajuste extra de
> configuração, o Launch da web UI da Fly — veja abaixo). Os dois caminhos são
> validados.

## O que é provisionado

- **omnicraft** — uma machine que baixa `ghcr.io/omnicraft-ai/omnicraft-server`,
  servida em `https://<app>.fly.dev`.
- **artifact_data** — um volume persistente montado em `/data/artifacts`,
  guardando o armazenamento de artefatos, o cookie secret gerado, e (por
  padrão) o banco de dados SQLite.

O `fly.toml` padrão usa **SQLite no volume** — sem app de banco separado. Isso
é persistente entre restarts e serve bem para uma única instância. Para
múltiplas instâncias, aponte `DATABASE_URL` para uma URL do Postgres (veja
abaixo).

## Publicar (CLI — o caminho principal)

A autenticação embutida `accounts` (multiusuário, sem IdP externo) é o padrão.

```bash
# a partir da raiz do repositório
fly apps create <your-app>                                  # nome único globalmente
fly volumes create artifact_data --size 1 --region iad -a <your-app>   # combine com a região do fly.toml
fly deploy -c deploy/fly/fly.toml -a <your-app>
```

Depois:

1. **Memória** — o `fly.toml` fixa uma machine de **1 GB**
   (`[[vm]] memory = "1gb"`). O servidor fica ocioso em torno de ~275 MB de
   RSS, então o padrão de 256 MB da Fly entra em OOM-loop. Mantenha em 1 GB
   (ou `fly scale memory 1024 -a <your-app>` se você mudou isso).
2. A **senha do admin** aparece uma vez nos logs do primeiro boot:
   ```bash
   fly logs -a <your-app>
   ```
   Procure por `Created initial admin account ... password: <generated>`
   (também escrito em `/data/admin-credentials` no volume).
3. Abra `https://<your-app>.fly.dev`, entre como `admin`. O secret do cookie e
   a URL base (`FLY_APP_NAME` -> `<app>.fly.dev`) são tratados automaticamente.

## Publicar (Launch da web UI da Fly)

O Launch da web da Fly *constrói* uma imagem e a envia para o registry próprio
da Fly — ele não tem um modo de "publicar esta imagem externa", então o
`[build] image = ...` padrão dá 404 lá. Para usar a web UI, troque o
`fly.toml` para construir o shim de uma linha:

```toml
[build]
  dockerfile = "deploy/docker/Dockerfile.prebuilt"
```

O shim é `FROM ghcr.io/omnicraft-ai/omnicraft-server` sem nada adicionado,
então a Fly **baixa a imagem pré-construída e a retagueia** — sem rebuild do
código-fonte. O Launch ainda não cria o volume `artifact_data` automaticamente
nem aumenta a memória, então crie o volume (acima) e confirme 1 GB depois que
o Launch terminar.

## Use Postgres em vez de SQLite

Para múltiplas instâncias ou backups gerenciados, use Postgres em vez do
SQLite no volume. Duas opções:

- **Fly Postgres:**
  ```bash
  fly postgres create
  fly postgres attach <pg-app-name> -a <your-app>    # define DATABASE_URL como um secret
  ```
- **Neon (Postgres serverless):** crie um em [pg.new](https://pg.new) (entre
  com uma conta para mantê-lo), depois
  `fly secrets set DATABASE_URL='postgres://...' -a <your-app>`.

De qualquer forma, remova a linha `DATABASE_URL = "sqlite:..."` de `[env]`
para que o valor anexado/secret prevaleça. O entrypoint normaliza a URL
`postgres://` automaticamente.

> **Aumente a tolerância do healthcheck para um banco remoto.** O primeiro
> boot contra um Postgres externo (Neon) roda as migrações pela rede e leva
> ~1 minuto; o padrão SQLite-no-volume é quase instantâneo. Se você trocar
> para um banco remoto, aumente o `grace_period` no bloco
> `[[http_service.checks]]` (20s -> ~90s) para que a Fly não mate a machine no
> meio da migração no primeiro deploy.

## Use seu próprio IdP em vez disso (OIDC)

Troque o provedor com `fly secrets set` (OIDC exige HTTPS, que a Fly fornece
em `*.fly.dev`):

```bash
fly secrets set \
  OMNICRAFT_AUTH_PROVIDER=oidc \
  OMNICRAFT_OIDC_ISSUER=https://github.com \
  OMNICRAFT_OIDC_CLIENT_ID=<client-id> \
  OMNICRAFT_OIDC_CLIENT_SECRET=<client-secret> \
  OMNICRAFT_OIDC_REDIRECT_URI=https://<your-app>.fly.dev/auth/callback \
  OMNICRAFT_OIDC_COOKIE_SECRET=$(openssl rand -hex 32) \
  -a <your-app>
```

Para o Google Workspace, também defina `OMNICRAFT_OIDC_ALLOWED_DOMAINS` para
restringir os logins ao seu domínio.

## Custo

Uma machine `shared-cpu-1x` de 1 GB mais um volume de 1 GB custa alguns
dólares por mês para uma instância com carga leve. Adicione um app Postgres só
se você sair do SQLite.
