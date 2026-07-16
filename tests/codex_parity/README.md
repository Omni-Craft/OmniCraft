# Testes de Paridade do Codex

Esta suíte verifica a integração do OmniCraft com o Codex rodando a fronteira
real que nos importa:

```text
OmniCraft CodexExecutor
  -> real codex app-server process
  -> mock OpenAI Responses API
```

A escolha importante é que os testes não mockam a API OmniCraft-para-Codex.
Eles sobem uma CLI Codex real e só substituem o endpoint de modelo upstream.
Isso significa que o teste cobre o comportamento JSON-RPC do app-server do
Codex, a serialização das requisições do Codex, notificações de retry,
notificações de streaming, e idas e vindas de ferramentas dinâmicas.

## Arquitetura

```text
pytest
  |
  | starts
  v
Rust sidecar: tests/codex_parity/sidecar
  |
  | uses upstream Codex test helper crate
  v
core_test_support::responses / WireMock
  ^
  | /v1/responses
  |
real codex app-server
  ^
  | JSON-RPC app-server protocol
  |
OmniCraft CodexExecutor
```

O sidecar em Rust existe porque os helpers de mock da API Responses do Codex
são código de suporte a teste em Rust no repositório público do Codex. Em vez
de reimplementar esse mock em Python, o sidecar puxa a crate de suporte a
teste upstream diretamente via Cargo:

```text
core_test_support = { git = "https://github.com/openai/codex.git", rev = "..." }
```

Isso mantém o formato de conexão da Responses falsa alinhado com o Codex
upstream. O pytest continua sendo dono dos cenários de teste e das
asserções; o sidecar só sobe o WireMock, serve as fixtures SSE enfileiradas,
e reporta as requisições capturadas.

A revisão é fixada em `tests/codex_parity/sidecar/Cargo.toml`, então o
harness de paridade é reproduzível sem exigir um submódulo do Codex versionado
no repositório. Atualizar a implementação da fixture upstream é um bump normal
de dependência do Cargo: mude o `rev` do Codex, atualize o `Cargo.lock`, e
rode os testes de paridade.

## Fluxo da Fixture

Cada teste passa uma lista de respostas de modelo para o sidecar:

```python
sidecar = codex_responses_sidecar(
    [
        [
            ev_response_created("resp-1"),
            ev_assistant_message("msg-1", "hello"),
            ev_completed("resp-1"),
        ]
    ]
)
```

Cada lista interna vira um corpo de resposta SSE. O Codex consome um corpo por
requisição `POST /v1/responses`. Cenários multi-turno enfileiram várias listas
internas, por exemplo uma chamada de ferramenta dinâmica seguida pela resposta
final do assistente depois que o OmniCraft devolve o resultado da ferramenta.

O sidecar imprime uma linha JSON `ready` com uma `base_url`. A fixture do
pytest passa essa URL para o `CodexExecutor` usando o caminho existente de
sobrescrita de gateway, então o Codex manda o tráfego de modelo para o sidecar
em vez de para a OpenAI.

Depois de um turno, o pytest pede ao sidecar as requisições capturadas por um
pequeno protocolo JSONL de stdin/stdout:

```json
{"op": "requests", "min": 1, "timeout_ms": 5000}
```

A resposta inclui campos estáveis úteis para as asserções de paridade: caminho
da requisição, headers selecionados, e corpo JSON.

## Cobertura

`test_codex_executor_parity.py` cobre o comportamento de turno observável pelo
executor:

- `sdk/python/tests/test_app_server_run.py`
  - path/model/input da requisição mock da Responses
  - uso explícito de token cruzando a fronteira do app-server
  - seleção da última mensagem de fase desconhecida
  - preferência pela fase `final_answer`
  - saída somente de `commentary` não virando a resposta final
  - eventos de Responses com falha surgindo como erros de turno
- `sdk/python/tests/test_app_server_streaming.py`
  - roteamento de delta de texto e resposta de turno completado
- comportamento selecionado de roteamento de requisição de
  `codex-rs/core/tests/suite/*`
  - ida e volta de chamada/resultado de ferramenta dinâmica pelo app-server
    real do Codex

`test_codex_goal.py` cobre o contrato de goal do Codex do qual o OmniCraft
depende:

- operações de goal do app-server upstream
  - ida e volta de `thread/goal/set` + `thread/goal/get` + `thread/goal/clear`
  - pause/resume via atualizações somente de status do `thread/goal/set`
  - preservação explícita de `tokenBudget: null`
  - `thread/goal/clear` idempotente
  - preservação de `budgetLimited` ao definir o mesmo objetivo
  - status de goal `blocked` e `usageLimited` persistidos
- rotas de goal do AP do OmniCraft
  - `PUT /v1/sessions/{id}/codex_goal` encaminha objective, budget e mode
  - `PATCH /v1/sessions/{id}/codex_goal/status` encaminha pause/resume
  - status terminais de propriedade do Codex são rejeitados como entradas
    graváveis pelo usuário
  - status terminais de propriedade do Codex devolvidos pelo runner são
    preservados
  - erros no formato da API devolvem 404s em JSON em vez do shell da SPA

Isso é abrangente para a superfície de goal que o OmniCraft possui porque
exercita os dois lados da integração: o JSON-RPC real do app-server do Codex
para toda transição de estado de goal da qual dependemos, e o mapeamento de
rotas HTTP públicas do OmniCraft para todo controle de navegador que
expomos. Intencionalmente não copia os testes de renderização de slash-menu
/ status da TUI do Codex; o OmniCraft não embute esse caminho de TUI. Também
não duplica os testes internos de contabilidade de extensão de goal do Codex,
exceto onde o resultado do app-server faz parte do contrato público do
OmniCraft.

Ainda não representado aqui: testes de app-server só de SDK upstream para
ciclo de vida, login, aprovações, steer/interrupt, entrada de imagem
local/remota, e entrada de skill. Essas APIs ainda não têm uma superfície
direta no `CodexExecutor` do OmniCraft, então precisam de análogos voltados
para o executor ou de um harness de compatibilidade de SDK separado antes de
poderem virar testes de paridade um-para-um.

## Atualizando a Partir do Upstream do Codex

A dependência da fixture upstream do Codex é fixada em
`tests/codex_parity/sidecar/Cargo.toml`:

```toml
core_test_support = { git = "https://github.com/openai/codex.git", rev = "..." }
```

Para atualizar o harness:

1. Atualize esse `rev` para o commit do Codex contra o qual você quer validar.
2. Atualize o `tests/codex_parity/sidecar/Cargo.lock` construindo ou testando
   o sidecar.
3. Inspecione o checkout do Codex fixado no cache git do Cargo, geralmente em
   `~/.cargo/git/checkouts/codex-*/<rev>/`.
4. Compare estes arquivos upstream com os arquivos de paridade locais:
   - `sdk/python/tests/test_app_server_run.py`
   - `sdk/python/tests/test_app_server_streaming.py`
   - `sdk/python/tests/test_app_server_goal_operations.py`
   - `sdk/python/tests/test_client_rpc_methods.py`
   - `codex-rs/app-server/tests/suite/v2/thread_resume.rs`
   - `codex-rs/ext/goal/tests/goal_extension_backend.rs`
   - `codex-rs/prompts/src/goals_tests.rs`
5. Porte os novos casos de goal do contrato público do app-server para
   `tests/codex_parity/test_codex_goal.py`. Mantenha os casos só de TUI
   classificados como intencionalmente excluídos, a menos que o OmniCraft
   passe a expor esse caminho.
6. Rode o arquivo de goal focado, depois a suíte de paridade completa:

```bash
pytest tests/codex_parity/test_codex_goal.py \
  --codex-parity \
  --codex-bin "$(which codex)" \
  -q

pytest tests/codex_parity \
  --codex-parity \
  --codex-bin "$(which codex)" \
  -q
```

## Executando

Rode contra a CLI do Codex no `PATH`:

```bash
pytest tests/codex_parity --codex-parity -v
```

Rode contra um binário explícito:

```bash
pytest tests/codex_parity --codex-parity --codex-bin "$(which codex)" -v
```

Compare múltiplas versões do Codex:

```bash
pytest tests/codex_parity \
  --codex-parity \
  --codex-bin /path/to/codex-old \
  --codex-bin /path/to/codex-new \
  -v
```

Você também pode definir `CODEX_TEST_BINS` como uma lista separada por
`os.pathsep`.

Na Databricks, use o proxy interno do PyPI ao sincronizar o ambiente de teste
Python:

```bash
uv --no-config run --frozen \
  --default-index https://pypi-proxy.cloud.databricks.com/simple/ \
  --extra dev \
  pytest tests/codex_parity --codex-parity --codex-bin "$(which codex)" -q
```

## Por Que Este Formato

Mockar a API OmniCraft-para-Codex testaria nossas suposições sobre o
protocolo do app-server do Codex. Esta suíte, em vez disso, deixa o Codex
definir esse contrato rodando a implementação real da CLI/app-server. Só o
salto final de rede é mockado, o que nos dá testes estáveis e determinísticos
enquanto ainda captura desvios de protocolo entre o OmniCraft e o Codex.
