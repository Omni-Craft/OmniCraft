# Publicando o OmniCraft no Databricks Apps

Este diretório publica o servidor OmniCraft no
[Databricks Apps](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/)
via [Databricks Asset Bundles](https://docs.databricks.com/aws/en/dev-tools/bundles/):

- **Lakebase** (PostgreSQL gerenciado) — o banco de dados para todo o
  armazenamento.
- **UC Volumes** — o armazenamento de artefatos para bundles de agente e
  snapshots de armazenamento do executor.

> **A maioria dos usuários do Databricks quer a oferta gerenciada em vez
> disso.** O [OmniCraft no Databricks](https://docs.databricks.com/aws/en/omnicraft/)
> (Beta) roda o servidor por você, ligado à identidade do workspace, aos
> Foundation Models, ao AI Gateway e ao MLflow Tracing prontos de fábrica.
> Ative a prévia do **OmniCraft** nas configurações do seu workspace e siga o
> quickstart de lá. Use este diretório só quando você precisar autogerenciar
> o deploy: o serviço gerenciado ainda não está na sua região, ou você
> precisa de um controle que ele ainda não expõe hoje (políticas YAML
> customizadas, chaves de API de provider próprias, controles de egress
> customizados).

O orquestrador em `deploy.py` constrói as wheels, gera um `pyproject.toml` +
`uv.lock` do app, e então roda `databricks bundle deploy` + `bundle run`
contra a configuração do bundle em `databricks.yml`. A configuração do app
(Lakebase, UC volume) vive de forma declarativa em `databricks.yml` —
adicionar ou remover um recurso é uma edição de YAML, não uma chamada ao SDK
Python.

Roda sem alteração a partir de um notebook. Reexecutável; todo passo é
idempotente.

## Pré-requisitos

1. Um workspace Databricks com Databricks Apps, Lakebase e UC Volumes
   habilitados.
2. A [CLI do Databricks](https://docs.databricks.com/aws/en/dev-tools/cli/install.md)
   instalada e autenticada. Ou um perfil de CLI
   (`DATABRICKS_CONFIG_PROFILE=<profile>`) ou autenticação por variável de
   ambiente (`DATABRICKS_HOST` + `DATABRICKS_CLIENT_ID` +
   `DATABRICKS_CLIENT_SECRET`).
3. O venv local do repositório com o extra `databricks`:
   `uv sync --extra databricks` (use `uv`, não o pip global).
4. Permissões para criar ou usar:
   - um projeto Lakebase (um por app — não compartilhe com outros apps);
   - um volume UC cujo catálogo/schema pai possa conceder acesso ao service
     principal do app;
   - (opcional) secrets do Databricks para chaves de API de LLM.

Defina a URL do seu workspace em `databricks.yml`, em
`targets.prod.workspace.host` (ele vem com um placeholder
`https://example.databricks.com`; o DAB lê `workspace.host` antes de
resolver variáveis, então precisa ser um literal).

## Bootstrap único

### 1. Projeto Lakebase (um por app — nunca compartilhe)

Reaproveitar um projeto de autoscaling compartilhado faz o hook de migração
no boot falhar com "permission denied for table agents", porque as tabelas
pertencem a quem rodou as migrações primeiro. Sempre comece do zero:

```python
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.postgres import Project

wc = WorkspaceClient(profile="<your-profile>")
wc.postgres.create_project(project=Project(), project_id="omnicraft")

branch = "projects/omnicraft/branches/production"
endpoint = f"{branch}/endpoints/primary"

import time
for _ in range(120):
    ep = wc.postgres.get_endpoint(name=endpoint)
    if ep.status and ep.status.current_state == "ACTIVE":
        break
    time.sleep(5)
else:
    raise TimeoutError(endpoint)

database = next(iter(wc.postgres.list_databases(parent=branch)))
print("database resource path:", database.name)
```

### 2. UC Volumes

```sql
CREATE SCHEMA IF NOT EXISTS main.omnicraft;
CREATE VOLUME IF NOT EXISTS main.omnicraft.artifacts;
```

O volume `artifacts` é referenciado declarativamente em `databricks.yml` (o
recurso do app) via `--var volume_name=…`.

### 3. Primeiro deploy — cria o app e o seu service principal

Rode o comando de [Deploy](#deploy) uma vez. O primeiro `databricks bundle
deploy` cria o app e provisiona o seu service principal (SP). Esse primeiro
passo **não** vai passar no seu healthcheck `/health` ainda: o SP não tem
grants de schema no Lakebase, então o hook de migração no boot falha com
`permission denied for schema public`. Isso é esperado — o próximo passo
concede esses privilégios.

### 4. Conceda ao SP do app os privilégios do Lakebase

Agora que o app (e o seu SP) existem, conceda ao SP os privilégios do schema
public que o Alembic precisa, e então rode o deploy de novo:

```bash
python deploy/databricks/grant_sp_perms.py \
    --app-name omnicraft \
    --lakebase-endpoint projects/omnicraft/branches/production/endpoints/primary \
    --database databricks_postgres \
    --profile <your-profile>
```

> [!NOTE]
> O Lakebase usa duas grafias para o mesmo banco. O **caminho de recurso**
> usa um slug com hífen — `…/databases/databricks-postgres` (o que
> `deploy.py --lakebase-database` e `databricks.yml` querem) — enquanto o
> **nome do banco PostgreSQL** subjacente usa underscore,
> `databricks_postgres` (o que `grant_sp_perms.py --database` e o
> `PGDATABASE` do app usam). Passe cada forma onde indicado.

Depois desse grant único, rodar o deploy de novo sobe o app limpo e o
`/health` responde 200. Redeploys seguintes são uma única chamada a
`deploy.py`.

## Deploy

```bash
uv run python deploy/databricks/deploy.py \
    --app-name omnicraft \
    --profile <your-profile> \
    --lakebase-branch projects/omnicraft/branches/production \
    --lakebase-database projects/omnicraft/branches/production/databases/databricks-postgres \
    --volume-name main.omnicraft.artifacts
```

O script constrói as wheels, as classifica por tamanho, copia as wheels para
`src/`, regenera `src/pyproject.toml` e `src/uv.lock`, roda `databricks
bundle deploy --target prod`, roda `databricks bundle run omnicraft --target
prod`, e faz polling em `/health` com backoff até 200.

Todas as wheels do OmniCraft precisam caber sob o limite de snapshot de
origem do Databricks Apps (10 MB). Se uma wheel ultrapassar isso, reconstrua
com `--skip-web-ui` ou reduza o tamanho da wheel; lockfiles do uv não
conseguem apontar para caminhos de wheel em UC Volume porque o `uv lock`
valida path sources localmente.

Rodar de novo é seguro — todo passo é idempotente.

> [!TIP]
> Para travar contra um mirror ou proxy de PyPI privado em vez do PyPI
> público, defina `UV_INDEX_URL` antes de rodar `deploy.py`.

## Verificação rápida

O `deploy.py` faz polling em `/health` automaticamente. Para checar outros
endpoints:

```bash
TOKEN="$(databricks auth token <your-profile> --output json \
    | python -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')"

curl --http1.1 -fsS \
    -H "Authorization: Bearer ${TOKEN}" \
    https://<app>.databricksapps.com/health
```

## Como funciona

### Autenticação

O app roda como um service principal do Databricks. As credenciais são
gerenciadas automaticamente:

- **Lakebase** — tokens OAuth gerados via
  `WorkspaceClient.postgres.generate_database_credential()`, injetados em
  toda conexão SQLAlchemy nova via um hook de evento `do_connect` de nível de
  classe em `src/app.py`.
- **UC Volumes** — credenciais de workspace usadas pelo SDK do Databricks
  (ambiente nos Apps).
- **Acesso via TUI / API** — OAuth baseado em navegador usando o cliente
  OIDC do `databricks-cli` com PKCE.

O proxy do Databricks Apps injeta `X-Forwarded-Email` em toda requisição,
então o app fixa `OMNICRAFT_AUTH_PROVIDER=header` (veja `src/app.py`).

> [!IMPORTANT]
> A autenticação por header confia no header `X-Forwarded-Email` ao pé da
> letra. Isso só é seguro **porque** a plataforma Databricks Apps termina a
> autenticação no seu proxy, remove qualquer cópia do header vinda do
> cliente, e a porta do app nunca é alcançável exceto através desse proxy.
> Não exponha o processo do app diretamente (por exemplo, um port forward ou
> um ingress alternativo que pule o proxy): um chamador que consiga definir o
> header ele mesmo poderia então se passar por qualquer usuário. Se você
> colocar o app atrás de qualquer coisa além do proxy padrão dos Apps,
> garanta que ela também higienize o header.

### Ciclo de vida do token

Os tokens OAuth do Lakebase expiram depois de 60 minutos. O pool de conexões
do SQLAlchemy recicla conexões a cada 5 minutos por padrão (configurável via
`AP_POOL_RECYCLE_SECONDS`), garantindo tokens novos em conexões novas.

### Armazenamento

| Componente | Backend | Finalidade |
|---|---|---|
| Specs de agente, tasks, conversas | Lakebase (PostgreSQL) | Metadados duráveis |
| Bundles de agente, snapshots do executor | UC Volumes | Armazenamento de blob binário |
| Estado de workflow do DBOS | Lakebase (mesmo banco) | Recuperação de workflow |
| Diretórios de trabalho do executor | Disco local efêmero | Cache (restaurado a partir de UC Volumes) |

## Referência de configuração

Variáveis de ambiente lidas por `src/app.py`:

| Variável | Origem | Descrição |
|---|---|---|
| `PGHOST` | Runtime do Databricks | Hostname do Lakebase |
| `PGPORT` | Runtime do Databricks | Porta do Lakebase (padrão 5432) |
| `PGDATABASE` | Runtime do Databricks | Nome do banco Lakebase |
| `PGUSER` | Runtime do Databricks | Usuário do Lakebase (service principal) |
| `PGSSLMODE` | Runtime do Databricks | Modo SSL (padrão `require`) |
| `AP_LAKEBASE_ENDPOINT` | recurso do app `valueFrom: postgres` | Endpoint do Lakebase para geração de token |
| `AP_ARTIFACT_VOLUME_PATH` | recurso do app `valueFrom: artifact_volume` | Caminho do UC Volume para artefatos |
| `DATABRICKS_APP_PORT` | Runtime do Databricks | Porta do app (padrão 8000) |
| `AP_POOL_RECYCLE_SECONDS` | Opcional | Intervalo de reciclagem do pool de conexões (padrão 300) |

## Segurança multi-app — um bundle, muitos apps

O mesmo diretório de bundle pode publicar vários apps (um por
`--app-name`). O Terraform só consegue apagar ou substituir o que está
rastreado no estado que ele carrega, então o raio de impacto de um deploy é
exatamente aquele arquivo de estado.

- **O estado remoto é por app.** `targets.<t>.workspace.root_path` termina
  em `${var.app_name}`, então `--app-name X` lê e escreve estado só sob
  `.bundle/omnicraft/X`. Um deploy de X não consegue alterar o app Y.
- **O `name` do recurso do app é `${var.app_name}`.** Se o estado carregado
  rastreia o app X mas você passa `app_name=Y`, o terraform vê uma mudança de
  nome e planeja um **destroy de X + create de Y**. Nunca vincule o recurso
  do bundle a um app e depois faça deploy com um `--app-name` diferente.
- O cache **local** em `deploy/databricks/.databricks/bundle/<target>/` é
  por *target*. Antes de publicar um app *diferente* num target já usado
  antes, remova-o:
  `rm -rf deploy/databricks/.databricks/bundle/<target>`
  (é só um cache; o estado remoto por app é a fonte da verdade).

Se um plano de `bundle deploy` mostrar um delete ou replace de um
`databricks_app`, aborte e reconfira o bind e o `--app-name` — redeploys de
rotina só atualizam no lugar.

## Modos comuns de deploy

```bash
# Iterar sem reconstruir as wheels (reaproveita dist/; útil quando só mudou
# app.py / app.yaml). Pula a checagem de árvore limpa.
uv run python deploy/databricks/deploy.py --skip-build --allow-dirty ...

# Deploy só de API (tira a SPA da wheel principal).
uv run python deploy/databricks/deploy.py --skip-web-ui ...
```

## Troubleshooting

| Sintoma | Causa | Correção |
|---|---|---|
| Deploy recusa: "working tree has uncommitted changes" / "HEAD is not at origin/main" | Verificação de árvore limpa | Faça commit/stash, `git checkout main && git pull`, ou passe `--allow-dirty` |
| `bundle deploy` falha: "Resource already managed by Terraform" | App já vinculado a outro diretório de bundle | Rode a partir daquele diretório, ou desvincule: `databricks bundle deployment unbind omnicraft` |
| `bundle deploy` falha: "An app with the same name already exists" | App existe mas não está vinculado a este bundle (ou um cache local por target obsoleto, de um app *diferente*, fez o `deploy.py` pular o bind) | `rm -rf deploy/databricks/.databricks/bundle/<target>`, depois vincule: `databricks bundle deployment bind omnicraft <app-name> --target <target> --auto-approve --var ...` |
| App falha com "Error installing packages"; `/logz` mostra "Ignoring existing lockfile due to … exclude newer …" e depois um timeout no fetch do PyPI | O runtime dos Apps fixa um corte global `exclude-newer` do uv; um lock gerado sem essa opção correspondente é re-resolvido dentro do container, onde o PyPI é inalcançável | Leia o corte em `/logz` ("change of exclude newer timestamp from X to Y") e faça o redeploy com `UV_EXCLUDE_NEWER=<cutoff>` no ambiente |
| `permission denied for table agents` | Tabelas do Lakebase pertencem ao usuário errado | Conecte como o dono e `DROP TABLE … CASCADE`; redeploy |
| `schema "dbos" already exists` | O mesmo, para o schema do DBOS | `DROP SCHEMA dbos CASCADE` e redeploy |
| `permission denied for schema public` | SP do app sem grants de schema | Rode `grant_sp_perms.py` (uma vez) |
| `Field 'spec.role' cannot be empty` | Lakebase exige role explícita para bancos extras | Use o banco padrão do projeto; não crie extras |
| Deploy recusa porque uma wheel está acima de 10 MB | Payload do app do uv exige path sources locais de wheel | Reconstrua com `--skip-web-ui` ou reduza o tamanho da wheel |
| App sobe limpo mas a primeira requisição de agente dá 403 no volume de artefatos | SP do app tem `WRITE_VOLUME` na folha mas não `USE_CATALOG` / `USE_SCHEMA` nos pais | O `deploy.py` concede os dois automaticamente — para um catálogo novo, redeploy ou conceda manualmente via `databricks grants update` |

## Arquivos neste diretório

| Arquivo | Finalidade |
|---|---|
| `deploy.py` | Orquestrador. Ponto de entrada único. |
| `databricks.yml` | Configuração do bundle DAB. Declara o app + os seus recursos. |
| `build.sh` | Limpa os estáticos, constrói a web UI, constrói três wheels. |
| `grant_sp_perms.py` | Grant único do schema `public` do Lakebase para o SP do app. |
| `src/app.py` | O processo do app. Hook de token `do_connect` do SQLAlchemy + Alembic no boot + uvicorn. |
| `src/app.yaml` | Configuração de startup do app — comando + conexão de env-var. |
| `src/pyproject.toml` / `src/uv.lock` | Regenerados a cada deploy; não versionados (fixam a versão de wheel por deploy). |

## Veja também

- [`databricks.yml`](./databricks.yml) — configuração do bundle DAB.
- [Documentação do Databricks Apps](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/).
- [Documentação do Databricks Asset Bundles](https://docs.databricks.com/aws/en/dev-tools/bundles/).
