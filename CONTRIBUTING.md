# Contribuindo com o OmniCraft

Obrigado pelo interesse em melhorar o OmniCraft. Issues e pull requests são
bem-vindos. Para mudanças maiores, abra uma issue primeiro para discutirmos a
abordagem.

Por favor, não inclua segredos, URLs internas, dados de clientes ou
configurações privadas em issues, testes, exemplos ou logs.

## Configuração do ambiente de desenvolvimento

Este é um pacote Python com um frontend opcional em `web/`. Use o
[`uv`](https://docs.astral.sh/uv/) para o desenvolvimento local:

**SO de desenvolvimento suportado: macOS ou Linux.** Windows nativo não é
suportado para desenvolvimento — algumas dependências de teste são
POSIX-only (`pexpect`/`pyte` são excluídas no Windows), alguns módulos
importam bibliotecas padrão POSIX ou chamam `os.getuid()` durante o import, e
os hooks do `pre-commit` assumem o layout Unix de `.venv/bin/`, então
`pytest` e `pre-commit` não conseguem rodar nativamente. No Windows, use
**WSL2 (Ubuntu)** e clone no sistema de arquivos **Linux** (`~/…`, não
`/mnt/c`); isso corresponde ao CI. O Git Bash não é suficiente — ele executa
o Python nativo do Windows.

Instale primeiro os pré-requisitos locais:

- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) para
  ambientes Python e gerenciamento de dependências.
- `tmux`, necessário para os terminais nativos de Claude/Codex lançados pelo
  host local (`brew install tmux` no macOS, ou `apt install tmux` no
  Debian/Ubuntu).
- `bubblewrap` (`bwrap`), **somente Linux**, usado para isolar (sandbox) no
  nível do SO esses terminais nativos de Claude/Codex/Pi (`apt install
  bubblewrap` no Debian/Ubuntu). O macOS usa o sandbox `seatbelt` embutido e
  não precisa de nada extra.
- Node.js 22 LTS ou mais recente, com `npm`, ao trabalhar em `web/`.

```bash
git clone https://github.com/Omni-Craft/OmniCraft.git
cd OmniCraft

uv python install
uv venv --python "$(cat .python-version)"
uv sync --extra all --extra dev
source .venv/bin/activate    # ou prefixe os comandos com `uv run`
```

Verificações comuns:

```bash
uv run pytest                      # testes Python (e2e/live são pulados por padrão)
uv run ruff check . && uv run ruff format --check .
uv run pre-commit run --all-files
```

Ao mexer em `web/`:

```bash
cd web && npm install && npm run lint && npm run build
```

## Executando localmente

Para experimentar suas mudanças, inicie um servidor local, registre sua
máquina como host e rode o servidor de desenvolvimento do frontend. Use três
terminais separados:

```bash
# Terminal 1: servidor local na porta :6767
omnicraft server

# Terminal 2: registre sua máquina como host
omnicraft host --server http://localhost:6767

# Terminal 3: servidor de desenvolvimento do frontend
cd web
npm run dev
```

Abra a URL do Vite exibida pelo servidor de desenvolvimento do frontend,
geralmente `http://localhost:5173/`. É o registro do host que permite à
interface web navegar pelo seu sistema de arquivos e iniciar novas sessões na
sua máquina — sem ele, a interface web fica somente em modo leitura/continuação.

`omni` é um alias para `omnicraft`, então `omni host --server ...` também
funciona. A URL do host também pode ser passada posicionalmente (`omnicraft
host http://localhost:6767`). Veja o [README](README.md) para mais detalhes
sobre hosts, harnesses e credenciais.

### Validação local de desenvolvimento apenas do backend

Use isso quando quiser validar o backend Python e o servidor de API local a
partir de um checkout do código-fonte, sem construir a interface web,
configurar credenciais de provedor, criar sessões ou executar agentes —
uma verificação rápida de sanidade do servidor/API na sua cópia de trabalho
ou na `main` atual.

O script [`scripts/backend-smoke.sh`](scripts/backend-smoke.sh) automatiza
isso:

```bash
scripts/backend-smoke.sh              # sobe na porta 18080
PORT=18090 scripts/backend-smoke.sh   # sobrescreve a porta se a 18080 estiver ocupada
```

Ele instala o `uv` em um toolchain venv descartável, roda `uv sync
--frozen`, inicia o servidor em modo somente-API (`OMNICRAFT_SKIP_WEB_UI=true`),
aguarda o `/health`, e testa `/`, `/health`, `/docs`, `/v1/agents` e
`/v1/sessions` -- esperando HTTP `200` de todos os cinco. Ele encerra com
código de saída diferente de zero se alguma verificação falhar.

Observações:

- **Requer `bash` ou `zsh`** (o shebang `#!/usr/bin/env bash` do script
  garante isso); não é portável para `sh` POSIX. **Também precisa de**
  Python 3.12+ como `python3`, `git`, `curl`, e acesso à rede para o PyPI.
  Nenhuma credencial de provedor é necessária. **Funciona em Linux e macOS.**
- **Totalmente isolado e descartável:** todo artefato -- o toolchain e os
  venvs do projeto, config, dados, o banco SQLite, artefatos, logs, e os
  caches de `pip`/`uv` -- vive dentro de um diretório de execução criado por
  `mktemp -d` e removido ao final, então a execução nunca toca o seu
  `~/.omnicraft` real, `~/.config` / `~/Library`, ou os caches de pacotes.
  `HOME` é a principal alavanca de isolamento (redireciona `~/.config` no
  Linux e `~/Library` no macOS); as variáveis explícitas `UV_*` / `PIP_*` /
  `OMNICRAFT_*` fixam o toolchain e o estado da aplicação independente do SO,
  e as `XDG_*` são definidas para que uma `XDG_*` já exportada no seu shell
  não consiga redirecionar o estado de volta para o seu home real.
- **O que não é coberto:** a interface web, acesso mobile, fluxos de
  aprovação humano-no-loop, sessões apoiadas por provedores, ou execução de
  agentes. Use o fluxo completo de desenvolvimento local acima ao trabalhar
  nessas áreas.

## Testes

Uma mudança que altere o comportamento sob `omnicraft/` deve vir acompanhada
de um teste, e uma correção de bug deve adicionar um teste que falhe antes da
correção. Refatorações puras, renomeações, mudanças apenas de tipo, updates
de dependências, e edições sem mudança observável de comportamento não
precisam de um novo teste.

Prefira o menor teste que cubra a mudança. Um **teste unitário** rápido e
focado na suíte da área correspondente é o padrão e o que a maioria das
mudanças precisa. Recorra a `tests/integration/` somente quando o
comportamento genuinamente abranger vários componentes, e a `tests/e2e/`
apenas para fluxos full-stack que um teste unitário não consiga capturar —
esses são mais lentos e (no caso do e2e) dependentes de gateway, então não os
use onde um teste unitário resolveria.

Coloque o teste na suíte que corresponde à área alterada — a maioria das
áreas do backend espelha seu diretório de origem em `tests/`:

| Área alterada (`omnicraft/…`) | Suíte de teste (`tests/…`) |
| --- | --- |
| `server/` | `server/` |
| `runner/` | `runner/` |
| `runtime/` | `runtime/` |
| `tools/` | `tools/` |
| `inner/` | `inner/` |
| `llms/` | `llms/` |
| `db/` | `db/` (uma migração de esquema merece especialmente um) |
| `policies/` | `policies/` |
| `repl/` | `repl/` |
| `entities/` | `entities/` |
| `stores/` | `stores/` |
| `host/` | `host/` |
| `spec/` | `spec/` |

Duas suítes transversais ficam acima dessas:

- `tests/integration/` — comportamento que abrange vários componentes (por
  exemplo, server + runtime) e não é capturado pelo teste unitário de
  nenhuma área isolada.
- `tests/e2e/` — fluxos full-stack executados contra um LLM real (sessões, o
  runtime, dispatch de sub-agentes, tunelamento de ferramentas de cliente,
  transportes, pontes de harness nativas, steering/cancelamento). Esses são
  lentos e dependentes de gateway, então reserve-os para comportamento
  genuinamente ponta a ponta — mas um PR que adicione nova funcionalidade
  voltada ao usuário **deve** incluir ao menos um teste e2e de caminho feliz
  (happy-path) (veja `.github/copilot-instructions.md`).

### Frontend (`web/`)

Mudanças de frontend seguem a mesma expectativa, com um toolchain diferente:

- Adicione ou atualize um **teste Vitest colocalizado** — um arquivo
  `*.test.ts`/`*.test.tsx` ao lado do componente ou módulo alterado — e rode
  com `npm test`.
- Uma mudança no **comportamento de UI voltado ao usuário** também precisa
  de um teste Playwright em `tests/e2e_ui/`. Isso é reforçado
  mecanicamente pela verificação `E2E UI Required`, então um PR de UI não
  será mesclado sem um teste que o cubra (ou uma dispensa de um mantenedor)
  — veja `.github/workflows/e2e-ui-required.yml`.
- Mudanças somente de estilo/formatação, ajustes de texto sem mudança de
  fluxo, e refatorações sem mudança de comportamento são isentos, assim como
  no backend.

## Pull requests

- Parta (branch) da `main`, mantenha as mudanças focadas, e inclua testes ou
  documentação quando pertinente.
- Assine seus commits com `git commit -s` (Developer Certificate of Origin).
- Preencha o template de PR. Para **mudanças de UI / frontend**, marque a
  caixa "UI / frontend change" e anexe um **vídeo ou imagens** na seção
  `Demo` mostrando o novo comportamento, para que os revisores possam vê-lo
  sem precisar fazer checkout da branch.
