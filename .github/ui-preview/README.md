# UI Preview

Publica uma prévia ao vivo, por PR, da web UI do OmniCraft como um
[Databricks App](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/)
quando um PR muda o frontend (`web/`).

## Como funciona

1. Um mantenedor adiciona o label `ui-preview` a um PR (o workflow só é
   liberado para autores `OWNER`/`MEMBER`/`COLLABORATOR`).
2. O [workflow do UI Preview](../workflows/ui-preview.yml) constrói o SPA + os
   wheels do OmniCraft e os publica num Databricks App efêmero
   (`omnicraft-ui-preview-pr-<N>`).
3. Um comentário com a URL da prévia é postado no PR e atualizado a cada push.
4. O app é apagado automaticamente quando o PR é fechado.

## O que é

Diferente do deploy de produção do OmniCraft no Databricks
(`deploy/databricks/`, apoiado em Lakebase Postgres + UC Volumes), a prévia é
intencionalmente efêmera e autocontida: um banco **SQLite** + um repositório
de artefatos em disco local, descartados no desmonte.

Não há **nenhum LLM ou runner embutido na prévia** -- o OmniCraft roda os
turns dos agentes num runner que o usuário conecta a partir da própria máquina
ou sandbox (`omnicraft run … --server <preview-url>`), onde ficam as
credenciais do modelo. Então a prévia serve para revisar o visual e a
navegação da UI; para conduzir uma sessão de verdade, conecte o seu próprio
host à URL da prévia.

## Acesso

Os apps de prévia só são acessíveis a mantenedores com acesso ao workspace do
Databricks (o proxy dos Apps injeta `X-Forwarded-Email`, então o app roda no
modo de autenticação por header).

## Configuração (única, feita por um mantenedor)

Adicione estes secrets do repositório:

- `DATABRICKS_HOST`
- `DATABRICKS_CLIENT_ID`
- `DATABRICKS_CLIENT_SECRET`

Crie um label `ui-preview`. Se o workspace usa allowlist de IP, cadastre um
runner de IP estático e aponte os jobs `deploy`/`cleanup` para ele.
