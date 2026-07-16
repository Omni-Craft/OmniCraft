# Benchmark de performance do OmniCraft

Números de latência/throughput de referência, repetíveis, para as principais
jornadas de usuário do OmniCraft, para que possamos acompanhá-los ao longo do
tempo e detectar regressões. Modelado no workflow `dev/benchmarks/gateway/` do
MLflow.

O harness sobe um `omnicraft server` de verdade, conduz as jornadas
selecionadas sob carga, imprime tabelas de latência/throughput, e grava um
relatório JSON versionado. Duas famílias: **jornadas HTTP/API** (servidor + BD,
sem runner/LLM — rápidas e com pouco ruído) e **jornadas de turn completo** (um
turn de agente de verdade através do runner + um LLM mock de latência zero).
Veja *Jornadas* abaixo.

Por padrão o servidor sobe com um BD SQLite novo e vazio, o que dá números de
melhor caso que não se movem com carga. Para resultados significativos, aponte
para um **corpus pré-semeado** (`seed.py`) e, idealmente, para **Postgres** —
a produção roda no Databricks Lakebase (Postgres), cujo custo de round-trip
por query + pooling o SQLite não tem. Veja *Semeadura* e *Backends* abaixo.

## Como rodar

```bash
# Todas as jornadas, latência sequencial (100 iterações × 3 execuções cada).
uv run --no-sync dev/benchmarks/omnicraft/run.py

# Um subconjunto, gravando um relatório para upload como artefato de CI.
uv run --no-sync dev/benchmarks/omnicraft/run.py \
    --journeys list_sessions,load_conversation_history \
    --iterations 200 --runs 3 --output bench.json

# Modo throughput: concorrência >1 conduz jornadas seguras para concorrência como carga.
uv run --no-sync dev/benchmarks/omnicraft/run.py \
    --requests 500 --concurrency 25 --runs 3

# Gate de CI: sai com 1 se um limite for violado.
uv run --no-sync dev/benchmarks/omnicraft/run.py --max-p50-ms 25 --max-p99-ms 100
```

`--no-sync` roda contra o venv já instalado. (Um `uv run` puro pode tentar
reconstruir o projeto, o que falha num worktree do git sem um build Node da
web UI; `OMNICRAFT_SKIP_WEB_UI=true uv sync` prepara o venv uma vez, depois use
`--no-sync`.)

Flags principais (`--help` para todas): `--journeys A,B`,
`--database-uri URI` (corpus semeado / Postgres; padrão: SQLite vazio
descartável), `--iterations N` (por execução de latência), `--requests N` /
`--concurrency N` (throughput), `--runs N`, `--warmup N`, `--output FILE`,
`--min-rps` / `--max-p50-ms` / `--max-p99-ms` (limites de CI).

## Jornadas

### HTTP/API (servidor + BD, sem runner)

| Jornada | Operação medida | Estressada por |
| --- | --- | --- |
| `list_sessions` | `GET /v1/sessions` — leitura da lista de sessões | contagem de sessões |
| `create_session` | `POST /v1/sessions` depois `DELETE` — criação de sessão | caminho de escrita |
| `get_session` | `GET /v1/sessions/{id}` — snapshot de uma sessão | (O(1)) |
| `load_conversation_history` | `GET /v1/sessions/{id}/items` — leitura de histórico | items/sessão |
| `search_sessions` | `GET /v1/sessions?search_query=` — `LIKE` sem índice | contagem total de items |
| `fork_session` | `POST /v1/sessions/{id}/fork` — fork (deep-copy de items); forks apagados no teardown, não medidos | items/sessão |
| `add_comment` | `POST /v1/sessions/{id}/comments` — cria um comentário de revisão | caminho de escrita |

Jornadas de leitura miram uma sessão **pré-semeada** quando o BD tem um
corpus; contra um BD vazio elas se autossemeiam com uma pequena sessão
fallback via HTTP (o evento `external_conversation_item` — anexa items sem
iniciar uma task), então continuam funcionando sem runner nem LLM.

### Turn completo (runner + LLM mock)

Elas conduzem um turn de agente de verdade, ponta a ponta — `POST …/events` →
servidor → **runner** → executor in-process → LLM mock → stream de volta →
`idle`. Selecionar qualquer uma delas sobe automaticamente o
`BenchEnvironment(with_runner=True)`.

Cada turn custa ~1 s+ (contra os milissegundos das jornadas HTTP), então essas
jornadas limitam suas iterações de latência (`Journey.max_iterations`,
atualmente 5) — um `--iterations` grande ajustado para as jornadas HTTP é
reduzido para elas, para que a execução caiba no orçamento de tempo da CI, com
`--runs` fornecendo as repetições. O limite só reduz a contagem, nunca
aumenta. Um cold start nunca apaga a sua sessão, então as sessões se acumulam
ao longo de uma execução; manter a contagem pequena também mantém essa deriva
desprezível (~2 ms/turn).

| Jornada | Operação medida |
| --- | --- |
| `session_cold_start` | Cria e vincula uma sessão nova e conduz o primeiro turn dela até `idle` (spawn do runner + construção do executor + turn) |
| `warm_turn` | Conduz um turn numa sessão já aquecida — overhead de dispatch em regime estacionário |
| `time_to_first_token` | Posta um turn; tempo até o primeiro delta `output_text` transmitido |
| `interrupt` | Interrompe um turn em execução (com gate); tempo até o cancelamento |
| `read_runner_file` | `GET .../environments/default/filesystem/{path}` — proxy servidor → runner de leitura do sistema de arquivos |

`read_runner_file` precisa de um runner mas **não** conduz um turn nem chama o
LLM: sua preparação planta um arquivo via `PUT`, e a operação medida é a
leitura via proxy (um round-trip para localhost). Sendo bem mais barata que um
turn, ela usa um limite de iterações mais alto (50) do que as jornadas de turn
completo.

**Só medimos o que controlamos.** As jornadas de turn completo sempre usam o
harness de SDK **`openai-agents`**, que roda **in-process** (uma chamada para a
biblioteca `agents` + uma chamada HTTP para o LLM mock) — sem binário de
fornecedor, sem processo externo. Harnesses nativos (ex.: `claude-native`)
lançam a CLI real do fornecedor num pane do tmux, cujo start-up não
controlamos, então são deliberadamente excluídos. O LLM mock tem latência
zero, então todo número é overhead de dispatch/streaming/cancelamento do
omnicraft, não latência do modelo.

Adicione uma jornada registrando um `Journey` em `journeys.py` (defina
`needs_runner` para jornadas de turn completo).

## Semeando um corpus realista

`seed.py` grava um corpus grande e determinístico direto pela API do store
(sem HTTP, sem runner) no mesmo BD contra o qual o servidor então sobe:

```bash
# Semeia 5000 sessões × 50 items num arquivo SQLite, depois faz o benchmark contra ele.
uv run --no-sync dev/benchmarks/omnicraft/seed.py \
    --database-uri sqlite:////abs/path/bench.db --sessions 5000 --items-per-session 50
uv run --no-sync dev/benchmarks/omnicraft/run.py \
    --database-uri sqlite:////abs/path/bench.db --output bench.json
```

A semeadura é **idempotente**: um corpus correspondente (mesmas
sessões/items/schema) é detectado e reaproveitado, então rodar de novo é um
no-op rápido — passe `--reseed` para forçar, ou uma configuração diferente
para ser avisado. Caminhos absolutos de SQLite precisam de quatro barras
(`sqlite:////abs/...`). O marcador de reaproveitamento registra o head do
Alembic do BD lido no momento da semeadura, então um corpus de um schema mais
antigo é automaticamente re-semeado — sem controle manual de revisão.
`test_seed_creates_listable_corpus` (que semeia através do store, rodando as
migrações até o head atual) é a rede de segurança de que uma mudança de schema
não quebrou a semeadura.

## Backends

`--database-uri` seleciona o BD; o campo `backend` do relatório (`sqlite` /
`postgres`) é derivado do esquema da URI, então os resultados se agrupam por
backend.

- **SQLite** (padrão) — in-process; rápido, mas não representativo de
  produção.
- **Postgres** — `postgresql+psycopg://user@host:5432/db` (a forma totalmente
  qualificada `+psycopg`; a CLI do servidor não normaliza um `postgresql://`
  puro). Requer `psycopg[binary]` (o extra `databricks`). Corresponde ao
  perfil de round-trip/pooling da produção. Suba um local com
  `docker run -e POSTGRES_PASSWORD=… -p 5432:5432 postgres:16`.

## Saída → Databricks → dashboard

O harness só grava JSON. O armazenamento e a geração de gráficos vivem no
Databricks:

```
run.py --output bench.json   →   artefato do GitHub Actions   →   notebook do Databricks (ETL)   →   tabela Delta   →   dashboard AI/BI
        (este repositório)              (CI, follow-up)              (workspace, seu)
```

O contrato do repositório é o **schema JSON** abaixo. Um notebook do workspace
(mantido fora deste repositório, modelado no ETL do gateway do MLflow) puxa os
artefatos da CI pela API do GitHub, achata o `summary` + `runs` + metadados de
cada execução, e faz `saveAsTable` numa tabela Delta que o dashboard lê.
`sample_output.json` é um exemplo comitado e fiel, para que o notebook possa
ser escrito contra um documento real sem rodar o harness.

### Schema JSON (`schema.py`, `SCHEMA_VERSION`)

```jsonc
{
  "schema_version": 1,
  "generated_at": "<ISO-8601 UTC>",
  "git_sha": "<HEAD sha>",
  "git_branch": "<branch>",
  "host": {"platform": "...", "python": "...", "cpu_count": 12},
  "harness": "http-only",
  "config": {"iterations": 100, "requests": 500, "concurrency": 1,
             "runs": 3, "warmup": 10, "with_runner": false,
             "backend": "sqlite"},
  "journeys": {
    "<journey name>": {
      "kind": "latency" | "throughput",
      "backend": "sqlite" | "postgres",
      "runs": [                       // uma por --runs
        {"n_success": N, "n_failures": N, "failures": {"HTTP 500": 1},
         "wall_time_s": …, "mean_ms": …, "p50_ms": …, "p95_ms": …,
         "p99_ms": …, "max_ms": …, "rps": …}
      ],
      "summary": {"avg_mean_ms": …, "avg_p50_ms": …, "avg_p95_ms": …,
                  "avg_p99_ms": …, "avg_rps": …}    // média entre as execuções
    }
  }
}
```

O formato `summary` + `runs` por jornada espelha o benchmark de gateway do
MLflow, então o mesmo achatamento de ETL funciona — chaveado por `journey` e
`backend`. Suba o `SCHEMA_VERSION` em qualquer mudança de formato que quebre
compatibilidade, para que o notebook possa ramificar sobre isso.

## Estrutura

| Arquivo | Papel |
| --- | --- |
| `run.py` | orquestrador de CLI + entrypoint |
| `seed.py` | semeador determinístico de corpus (API do store) |
| `journeys.py` | dataclass `Journey`, executores de latência/throughput, registro |
| `environment.py` | ciclo de vida do servidor (± runner + LLM mock); `--database-uri` |
| `measure.py` | `RunResult`, percentil, agregação, limites, tabelas |
| `schema.py` | `SCHEMA_VERSION`, `build_report`, metadados de git/host |
| `sample_output.json` | exemplo comitado do contrato JSON |

O smoke test é `tests/benchmarks/test_benchmark_smoke.py` (sobe o servidor com
contagens minúsculas + um teste unitário de corpus semeado; roda na esteira
normal de CI, sem credenciais).

## CI

`.github/workflows/benchmark.yml` roda todo dia à noite (e sob demanda) como
uma matriz de backends — `sqlite` e `postgres` (um container de serviço
`postgres:16`). Cada perna semeia um corpus (o SQLite reaproveita um cache
chaveado no head do schema + `seed.py` + configuração do corpus, então uma
migração invalida o cache e força um re-semeamento; o Postgres é novo a cada
execução), roda o benchmark, e faz upload de
`benchmark-results-<backend>-<run_id>.json`. O notebook do workspace puxa
esses artefatos.

Mudanças de schema não precisam de passo manual: a semeadura sempre mira o
schema migrado atual (as migrações rodam quando o store é construído), o
marcador de reaproveitamento registra o head lido no momento da semeadura
(então corpora antigos são re-semeados automaticamente), e
`test_seed_creates_listable_corpus` falha se uma migração realmente quebrar a
semeadura.

## Próximos passos

- **Spawn de subagente.** Uma jornada de turn completo planejada
  (`needs_runner=True`): o agente pai emite uma chamada de ferramenta
  `sys_session_send`, o runner despacha uma sessão filha, e o pai acorda
  automaticamente com o resultado coletado. É totalmente mockável com o LLM
  mock de latência zero (sem modelo de verdade) — programe a fila do pai para
  emitir a chamada de ferramenta e a fila do filho para devolver uma resposta
  curta, depois faça polling pelo marcador do filho. Precisa que o bundle do
  pai declare um subagente em `tools:` (estenda `_agent_bundle`); o padrão está
  em `tests/e2e/test_coder_subagent.py`.
- **Jornadas excluídas** (dependentes do comportamento do agente,
  deliberadamente não medidas): turns multi-turn e com chamada de ferramenta
  (dominados pelas próprias escolhas do agente) e turns com histórico grande
  (a conversão O(N) de `history_to_input_items` é trabalho real do app, mas só
  dispara num cache de runner frio, então isolá-la se entrelaça com o custo de
  cold-start).
- **Matriz de CI.** As jornadas de runner são agnósticas de backend (elas
  exercitam o dispatch do runner, não leituras grandes de BD), então o
  workflow noturno pode rodá-las só na perna SQLite em vez das duas — conecte
  um conjunto `--journeys` de runner em `benchmark.yml` quando desejado.
- **Latência de provedor simulada.** O LLM mock retorna com latência
  praticamente zero, o que é o que isola o overhead do omnicraft. Um botão de
  atraso fixo por resposta permitiria que os turns modelassem o wall-clock do
  usuário final; é uma mudança pequena, atrás do gancho
  `configure_mock` / `set_mock_fallback`, caso isso venha a ser desejado.
