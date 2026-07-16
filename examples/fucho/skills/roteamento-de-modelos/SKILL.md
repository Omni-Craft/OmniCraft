---
name: roteamento-de-modelos
description: Pick the worker AND the `args.model` for each dispatch — the task→(worker, model) table, the cost ladder, and the mixing rules that make a run cheaper and better than sending everything to one model.
---

# roteamento-de-modelos — qual worker, qual modelo

You dispatch WORKERS, not bare models: each CLI is welded to its vendor, so a
routing decision is always a `(worker, args.model)` pair. This skill is the
table plus the mixing rules. It is a PRIOR, not a law — see "Quando ignorar".

## Regra geral (a mesclagem, em uma frase)

Strong model where a mistake is expensive (architecture, review, security,
big refactor) · `claude-sonnet-5` as the default executor for nearly
everything else · cheapest tier for pure volume · codex where the terminal IS
the product · gemini where context or multimodal is the bottleneck.

You are the "Opus plans" half already: your brain plans and decomposes, the
workers execute. So the default shape of a run is **one strong planner (you) +
sonnet-5 executors**, escalating only where the table says so.

## Tabela — tarefa → (worker, modelo)

| Tarefa | Worker + `args.model` |
| --- | --- |
| Implementação multi-arquivo, difícil | `claude_code` + `claude-opus-4-8` |
| Refatoração grande (não quebrar vizinhos) | `claude_code` + `claude-opus-4-8` |
| Tarefa longa autônoma, sem supervisão | `claude_code` + `claude-opus-4-8` |
| Review / auditoria de segurança (SAST) | vendor OPOSTO ao implementador, modelo forte: `claude_code` + `claude-opus-4-8` ou `codex` + `gpt-5.6-sol` |
| Implementação comum, executor padrão | `claude_code` + `claude-sonnet-5` |
| Debugging / brownfield, root cause | `claude_code` + `claude-sonnet-5` |
| Testes unitários / E2E em massa | `claude_code` + `claude-sonnet-5` |
| Boilerplate / scaffold / i18n PT-BR | `claude_code` + `claude-sonnet-5` (nunca Opus em tarefa mecânica) |
| Frontend/UI que precisa testar no browser | `claude_code` + `claude-sonnet-5` |
| Terminal / CI / automação / shell / DevOps | `codex` + `gpt-5.6-sol` |
| Classificação / extração em massa (JSON) | `codex` + `gpt-5.6-luna` ou `gemini` + `gemini-3.5-flash` |
| Explore / search / fan-out amplo e barato | `pi` (barato) ou `gemini` + `gemini-3.5-flash` |
| Prototipagem rápida / vibe coding | `claude_code` + `claude-sonnet-5` ou `gemini` + `gemini-3.5-flash` |
| Saída estruturada estrita (schema JSON) | `claude_code` ou `codex` — NÃO gemini (desvia mais do schema) |

Codex tiers: `gpt-5.6-sol` (forte) · `gpt-5.6-terra` (médio) · `gpt-5.6-luna`
(barato, volume). Claude tiers: `claude-opus-4-8` (forte; `claude-fable-5` se a
conta tiver) · `claude-sonnet-5` (executor padrão) · `claude-haiku-4-5` (volume).

## Escada de custo

1. **Explore / fan-out amplo** → o tier barato. Muitas chamadas, cada erro é
   descartável.
2. **Implementação comum** → `claude-sonnet-5`. É quase-Opus a preço de Sonnet;
   é o default, não uma economia arriscada.
3. **Implementação difícil / refactor / review final** → tier forte. Aqui um
   erro custa uma rodada inteira de re-trabalho, o que é mais caro que o token.

Escalar é decisão sua e não precisa de aval do humano; o que NÃO se faz é
mandar tudo no tier forte "por segurança" — isso queima limite e não melhora
tarefa mecânica.

## Regras de mesclagem

- **O revisor merece um modelo forte.** cross-review já força vendor diferente;
  esta skill acrescenta: dê ao revisor o tier forte DAQUELE vendor. Revisor
  fraco aprova diff ruim, e o custo do erro cai no merge.
- **Nunca revise com o mesmo modelo que implementou**, mesmo em worker
  diferente — o mesmo modelo repete o próprio ponto cego.
- **Um vendor por tarefa, não por run.** Misture entre tarefas (implementador
  Claude → revisor Codex; próxima tarefa pode inverter), não dentro da mesma
  tarefa.
- **Vendor diverso > vendor forte** para achar bug: dois vendedores diferentes
  em tier médio pegam mais que um vendedor forte sozinho.

## Quota do gemini — não apoiar volume nele

O `gemini` (acp:gemini-cli) roda com uma cota diária que estoura fácil sob
fan-out: quando a cota acaba, o worker morre com `runner_error` \"You have
exhausted your daily quota on this model\" — e a mesma conta derruba TODOS os
gemini seguintes no dia. Consequências práticas:

- NÃO use gemini como executor de VOLUME (fan-out amplo, classificação/extração
  em massa). Prefira `pi` ou `codex` + `gpt-5.6-luna` para volume; deixe o
  gemini para o caso pontual em que o edge de contexto/multimodal dele importa.
- Quando escolher gemini, tenha um vendor de fallback pronto: um
  `runner_error` de quota NÃO é bug de código nem falha de boot — é o
  fornecedor. Re-despachar no MESMO dia na mesma conta bate no mesmo muro;
  troque de vendor em vez de repetir.
- Distinga do crash de permissão headless (que aparece como
  `runner_disconnected`, corrigido com `permission_mode: bypassPermissions`):
  quota é `runner_error`/exhausted, disconnect é travamento. Só o primeiro é
  motivo para baixar o peso do gemini aqui.

## Procedimento

1. Antes de um fan-out (ou de escolher o revisor), classifique cada tarefa
   numa linha da tabela e anote o par `(worker, model)`.
2. Confirme com `sys_list_models` que o id existe NAQUELE worker — planos e
   contas mudam o que está disponível, e um id inválido falha loud no
   dispatch. Se o id sumiu, desça um tier do mesmo vendor.
3. Se `sys_advise_models` estiver na sua lista de ferramentas, chame-o com as
   tarefas planejadas e prefira a recomendação dele: ele lê o roteamento real
   deste deployment; esta tabela é o fallback quando ele não existe.
4. Passe o id em `args.model` no `sys_session_send`. Sem `args.model` o worker
   roda o default dele — aceitável para tarefa comum, desperdício para as
   pontas da escada.
5. Só recorra a um worker AVAILABLE no preflight. Um par perfeito num worker
   que não existe nesta máquina não vale nada.

## Quando ignorar a tabela

- **A memória do projeto ganha.** Se `memory_recall` disser que neste repo
  Sonnet quebra os testes de integração e só Opus fecha, siga a memória e não
  a tabela. Quando você descobrir um fato desses, `memory_remember`.
- **Benchmark varia por harness.** A tabela vem de benchmarks públicos
  (jul/2026); o seu repositório é a evidência real. Um worker que falhou duas
  vezes na mesma classe de tarefa desce na sua escolha, mesmo bem cotado.
- **O humano mandou.** Um pedido explícito de modelo vence tudo aqui.
