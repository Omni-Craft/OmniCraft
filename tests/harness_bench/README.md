# Bancada de testes de harness

Uma suíte de conformidade plugável que sonda o comportamento do harness e
reconcilia os vereditos observados com o modelo de capacidades para revelar
divergência (drift). Design e justificativa:
[`docs/harness-bench-design.md`](../../docs/harness-bench-design.md).

## Rodando

```bash
# Lista os harnesses oficiais (nome, transporte resolvido, modelo).
python -m tests.harness_bench --list

# Força a matriz só-declarada: sem turnos e sem credenciais necessárias.
python -m tests.harness_bench --no-live

# Sonda um harness. Credenciais são resolvidas como no `omni run`.
python -m tests.harness_bench --harness codex

# Sobrescreve o profile Databricks configurado/ambiente.
python -m tests.harness_bench --harness codex --profile my-profile

# Sonda vários harnesses concorrentemente com a tabela ao vivo.
python -m tests.harness_bench --jobs 4 --rich
```

Sem `--live` ou `--no-live`, a CLI roda ao vivo quando credenciais de gateway
são resolvíveis e, caso contrário, renderiza a matriz declarada offline. A
resolução de credenciais segue o `omni run`: o roteamento ambiente
`OPENAI_*` já existente é preservado; caso contrário, `--profile` sobrescreve
o profile configurado. Um exit não-zero significa que ao menos uma célula de
`DRIFT` foi encontrada.

### Flags

- `--live` / `--no-live` -- força a sondagem ao vivo ou a matriz
  só-declarada. `--live` exige credenciais de gateway resolvíveis.
- `--profile NAME` -- sobrescrita opcional do profile Databricks; não é
  necessária quando a config ou o `OPENAI_*` de ambiente já fornecem
  credenciais.
- `--harness NAME` -- sonda um harness (repetível). Aceita um nome oficial ou
  uma referência `module:attr` / `module.ATTR` a um `BenchProfile` da
  comunidade. O padrão é todo harness oficial.
- `--fast` -- roda harnesses de SDK no `sdk-inproc` em vez do padrão
  `full-server`. Isso pula o boot do servidor, mas os vereditos de política
  ALLOW/ASK/DENY não ficam observáveis e os vereditos de
  ferramenta/custo ficam limitados ao que o wrap encaminha. Não tem efeito
  em harnesses nativos e é mutuamente exclusivo com `--transport`.
- `--transport NAME` -- força `sdk-inproc`, `full-server` ou `native-tui`,
  sobrescrevendo o padrão da família do harness.
- `--jobs N` / `-j N` -- roda até N harnesses concorrentemente (padrão 1).
  As sondagens dentro de um harness continuam sequenciais e a ordem do
  relatório é estável.
- `--rich` / `--no-rich` -- força ou desliga a tabela de progresso ao vivo. O
  modo automático usa Rich num TTY e saída simples linha a linha caso
  contrário.
- `--report PATH` -- também escreve a matriz final. O formato segue o
  `--json` ou `--markdown`, depois a extensão do nome do arquivo.

### Formatos de saída

- Padrão: tabela de terminal alinhada mais Notes para toda célula
  não-suportada. A cor desliga automaticamente quando redirecionada ou com
  `--no-color`.
- `--markdown`: tabela no formato GitHub para docs e pull requests.
- `--json`: saída legível por máquina para comparar execuções ou regenerar
  docs.

Cada linha inclui o transporte que de fato rodou, como `claude-sdk
[full-server]` ou `kimi-native [native]`. Sob `--rich`, a tabela ao vivo é
renderizada no stderr; o relatório do stdout evita imprimir a grade duas
vezes, mas a saída redirecionada continua autocontida.

## Seleção de transporte

O `transport` de um profile é um marcador da família do harness. O driver
resolvido é:

- **Família SDK:** `full-server` por padrão. Isso roda por um servidor e
  runner reais e observa ferramentas despachadas pelo servidor mais o
  comportamento de política ALLOW/ASK/DENY fixo. `--fast` seleciona o
  driver `sdk-inproc` mais barato, direto no wrap.
- **Família nativa:** `native-tui`, que dirige uma CLI de vendor residente
  num pane tmux de propriedade do runner através da API de sessão do
  servidor.
- `--transport NAME` sobrescreve o padrão da família quando o driver suporta
  o harness selecionado.

## Dimensões

| Sondagem | O que verifica | Prioridade |
| --- | --- | --- |
| **Turno básico** | Um turno completa e retorna texto do assistente. | P0 |
| **Streaming** | Mais de um delta de output-text é emitido; um delta único repetido é `PARTIAL`. | P0 |
| **Chamada de ferramenta** | Uma chamada de ferramenta é exposta e o turno fecha depois do resultado dela. | P0 |
| **Política DENY** | Uma política de chamada de ferramenta bloqueia a chamada. | P0 |
| **Política ALLOW** | Uma chamada de ferramenta prossegue com uma política de allow explícita anexada. | P1 |
| **Política ASK** | Uma política de ask levanta uma elicitação de aprovação. | P1 |
| **Sobrescrita de modelo** | O harness aceita e completa com o modelo requisitado. | P0 |
| **Rastreio de custo** | Um turno completo reporta custo precificado (`SUPPORTED`) ou só tokens (`PARTIAL`). | P1 |
| **Interrupção** | Um turno em execução para depois de interrompido. | P0 |

Os vereditos são `SUPPORTED` (`✓`), `PARTIAL` (`~`), `UNSUPPORTED` (`✗`),
`NOT_APPLICABLE` (`—`), `UNKNOWN` (`?`), `SKIPPED` (`·`) e `DRIFT` (`!!`).
Um skip significa que a bancada não conseguiu medir o comportamento naquele
ambiente ou transporte; não afirma que o harness não tem a capacidade.

### Cobertura por transporte

| Dimensão | `full-server` | `native-tui` | `sdk-inproc` (`--fast`) |
| --- | --- | --- | --- |
| Turno básico, Streaming, Sobrescrita de modelo, Interrupção | Ponta a ponta por servidor + runner | Ponta a ponta por servidor + runner + CLI de vendor | Só o limite do wrap |
| Chamada de ferramenta | Builtin despachada pelo servidor | Ferramenta de vendor espelhada como um item de sessão | Wrap de ferramenta a nível de requisição |
| Política DENY | Política fixa no spec do agente | Política CEL de sessão + hook de política nativa | Não observável |
| Política ALLOW / ASK | Política fixa; ASK observa e resolve uma elicitação | Política CEL de sessão temporária; ASK observa e resolve uma elicitação | Não observável |
| Rastreio de custo | Snapshot de sessão | Snapshot de sessão quando o vendor encaminha uso | Uso da resposta completa quando encaminhado |

A bancada é um cliente headless da API do servidor. Ela verifica o contrato
que a aplicação web consome, não a renderização do navegador; a apresentação
de UI pertence a `tests/e2e_ui/`.

## Layout

| Arquivo | Papel |
| --- | --- |
| `verdict.py` | Vereditos, prioridades, resultados de sondagem e reconciliação de drift |
| `profile.py` | `BenchProfile` e resolução de nome de profile |
| `manifest.py` | Profiles oficiais derivados do registro de capacidades e dos metadados de sondagem e2e |
| `transport.py` | Protocolo de driver, registro e resolução de transporte |
| `driver.py` | `SdkInprocDriver`, `TurnResult` compartilhado e helpers de uso |
| `full_server.py` | Ciclo de vida compartilhado de servidor/runner e registro de agente/sessão |
| `full_server_driver.py` | Implementação de sondagem full-server e polling compartilhado |
| `native_tui_driver.py` | Provisionamento de CLI de vendor nativa e implementação de sondagem nativa |
| `session_items.py` | Parsing compartilhado para os formatos de envelope de item de sessão |
| `runtime_env.py` | Resolução de credencial/config compartilhada com o comportamento de runtime normal |
| `probes/` | Um módulo por dimensão; `ALL_PROBES` define ordem de exibição e execução |
| `events.py` / `richreport.py` | Eventos de progresso estruturados e renderização Rich opcional |
| `bench.py` | Orquestração, concorrência, tratamento de pré-requisito e conexão com servidor compartilhado |
| `report.py` | Renderizadores de terminal, Markdown e JSON |

Helpers de produção reutilizáveis ficam em `omnicraft.config` e nos módulos
de utilitário de runtime já existentes, em vez de duplicados na bancada.

## Estendendo a bancada

### Adicionar um harness

- **SDK oficial:** registre o harness normalmente; os metadados de sondagem
  base e as capacidades fluem para o manifesto sem precisar de um driver
  novo.
- **Nativo:** todo harness marcado `NATIVE_TUI` é derivado automaticamente;
  `native_vendor()` deriva os metadados de lançamento dele a partir das
  capacidades.
- **Comunidade:** publique um `BenchProfile` e selecione-o com
  `--harness mypkg.harness:PROFILE`.

Um harness nativo da comunidade é reconhecido pela bancada, mas o servidor
ainda precisa de um agente de UI nativa semeado. O seeding de agente nativo
guiado pelo registro continua sendo um item de plataforma em aberto.

### Adicionar uma dimensão

Adicione um `CapabilityProbe` em `probes/`, registre-o em
`probes/__init__.py:ALL_PROBES`, adicione um método semântico ao protocolo do
driver, e derive ou declare o veredito esperado. Mantenha a mecânica
específica de transporte dentro dos drivers para que as sondagens continuem
agnósticas de harness.

## Lacunas atuais

- O seeding de agente nativo no servidor ainda é hardcoded em vez de guiado
  pelo registro de harness, o que limita a execução ponta a ponta nativa da
  comunidade.
- Alguns harnesses nativos exigem login/configuração de provedor do vendor
  que a bancada não consegue provisionar e, por isso, pulam de forma limpa.
- Steering, fila ao vivo, resume/fork, reasoning, imagens e compaction ainda
  não têm sondagens.
