# Testes de snapshot visual da UI

Uma baseline de regressão visual versionada por página, cada uma capturada em
viewport cheio a 1280×800 com o esquema de cores fixado em `light`, gateadas
juntas na CI.

As chamadas de dados de cada página são stubadas via `page.route` com fixtures
fixas, então a view renderizada é uma função pura do bundle versionado e não
precisa de mascaramento de elemento. O `live_server` ainda serve o bundle da
SPA; só `/v1/info` / `/v1/me` alcançam o servidor real (e determinístico). As
partes compartilhadas (viewport fixo + paleta clara, o helper de rota JSON, o
assentamento de fontes/caret pré-captura) ficam em
[`conftest.py`](conftest.py), então cada `test_*_snapshot.py` só declara os
seus próprios stubs.

Páginas cobertas:

- **Landing vazia (`/`)** — a sidebar esquerda aberta mais o hero do
  `NewChatLandingScreen` ("O que devemos fazer?") e o composer.
  [`test_landing_snapshot.py`](test_landing_snapshot.py)
- **Conversa de chat (`/c/{id}`)** — um transcript de um turno totalmente
  mockado (pergunta do usuário + resposta em markdown do assistente)
  renderizado como bolhas de mensagem, com o composer abaixo.
  [`test_chat_snapshot.py`](test_chat_snapshot.py)

As baselines ficam versionadas em
`snapshots/<test_module>/<test_name>/<name>[chromium][linux].png`.

- Workflow do gate: [`.github/workflows/ui-snapshot.yml`](../../../.github/workflows/ui-snapshot.yml)
- Regeneração local (Docker): [`regen_baseline_docker.sh`](regen_baseline_docker.sh)
- Plugin: [`pytest-playwright-visual-snapshot`](https://github.com/iloveitaly/pytest-playwright-visual-snapshot)

## Por que um único renderizador fixo

Screenshots divergem entre ambientes de renderização (rasterizador de fonte,
hinting, anti-aliasing), e nenhum limiar de diff consegue reconciliar dois
motores de renderização diferentes. Então renderizamos tudo em **um só**
ambiente: uma imagem Playwright fixada por digest
(`mcr.microsoft.com/playwright/python`, que já traz Chromium + fontes). A CI
renderiza nela, e você pode reproduzir exatamente essa renderização
localmente com Docker — veja
[Atualizando a baseline](#atualizando-a-baseline). Como o renderizador é a
imagem, o SO do seu host não importa; você só precisa do Docker (ou deixa a
CI fazer isso).

O teste é marcado como `@pytest.mark.visual`; a suíte principal de e2e-ui
(`ubuntu-latest` sem fixação) o exclui via `-m "not visual"`. Só o
`ui-snapshot.yml` o roda.

## Essa checagem bloqueia merge?

A checagem **`UI Snapshot (visual baselines)`** só bloqueia merges se estiver
listada no conjunto de checagens obrigatórias do repositório (proteção de
branch / `.github/scripts/merge-ready`, que é gerado e sincronizado à parte).
Até ser adicionada lá, é uma checagem vermelha **consultiva** — visível, mas
não obrigatória. Registrá-la como obrigatória é uma mudança de uma linha
nessa configuração sincronizada, fora deste diretório.

É **seguro registrá-la como obrigatória**: um PR que não toca em nenhum dos
inputs de renderização pula a renderização pelo `if` do job `detect`, e um
job pulado por `if` reporta **sucesso** — então PRs não relacionados a UI
satisfazem a checagem em vez de ficar "pendentes" (que é o que um filtro
`on: paths:` causaria, bloqueando merges).

## Como o gate se comporta

- Em todo PR que toca um input de renderização (web, os testes visuais +
  fixtures, ou o toolchain fixado — veja o job `detect` em
  `ui-snapshot.yml`), o `ui-snapshot.yml` renderiza cada página e a compara
  com a sua baseline versionada. Qualquer diferença de pixel em qualquer
  página reprova a checagem; PRs que não tocam nenhum desses pulam a
  renderização (reportado como um skip que passa).
- **Toda execução (passa ou falha)** sobe um artefato e o linka no resumo do
  job, então os screenshots ficam sempre a um clique de distância:
  `ui-snapshot-<run_id>` carrega as renderizações desta execução
  (`snapshots/`); numa divergência, `snapshot_failures/` também guarda os
  PNGs `expected_` (baseline), `actual_` (atual) e `diff_` de cada página que
  falhou. Esse artefato único é baseline + atual + diff.
- As baselines **nunca** são alteradas pelo gate de comparação. Os únicos
  jeitos de alterá-las são os fluxos de atualização abaixo.

## Atualizando a baseline

Quando uma mudança de UI é intencional, escolha o caminho que fizer mais
sentido — todos renderizam na imagem fixada, então o resultado bate com o
gate. Os caminhos de label e Docker reescrevem **só** as baselines que
divergiram (ou estão faltando) e deixam as que já passavam intocadas
byte a byte. Revise cada imagem alterada antes de commitar.

### Branch no mesmo repositório — label no PR (recomendado)

1. Suba sua branch e abra o PR.
2. Adicione o label **`update-ui-snapshot`**.
   [`ui-snapshot-update.yml`](../../../.github/workflows/ui-snapshot-update.yml)
   re-renderiza na mesma imagem fixada, regenera só as baselines que
   divergiram (ou estão faltando) — as que já passavam ficam intocadas — e
   commita os PNGs alterados de volta na sua branch, depois remove o label e
   comenta o resultado.
3. **Revise o(s) PNG(s) commitado(s)** no commit do bot.
4. O bot faz o push com o token `OMNICRAFT_BOT_APP`, então o push
   redispara as checagens do PR automaticamente — sem re-run manual. (Se o
   App não estiver configurado, ele recai no `GITHUB_TOKEN`, que não
   redispara a CI; o comentário do bot avisa disso e você faz push de
   qualquer commit para rodar de novo.)

Isso funciona **só para branches no mesmo repositório** — tokens de Actions
não conseguem dar push num fork.

### Em qualquer lugar com Docker — regenerar localmente (funciona para forks)

```bash
tests/e2e_ui/visual/regen_baseline_docker.sh
```

Isso renderiza dentro da exata imagem fixada que a CI usa, então os PNGs
gerados batem byte a byte com o gate. Só precisa de Docker (ele builda a SPA
num container Node, depois renderiza a suíte e reescreve só as baselines que
divergiram — as que já passavam ficam intocadas). **Revise a(s) imagem(ns)**,
depois commite e dê push — seu push reroda as checagens. Passe
`--skip-build` para reaproveitar um build de `web` existente.

### PR de fork sem Docker — adotar a renderização da execução

A execução de comparação que falhou já renderizou sua mudança na imagem
fixada, e como roda sob GitHub Actions o plugin reescreveu cada baseline
divergente **no lugar** dentro de `snapshots/`. Traga essa árvore para cá:

```bash
tests/e2e_ui/visual/update_baseline_from_pr.sh <pr-number>
```

Ele encontra a execução de UI Snapshot do PR, baixa o artefato, e restaura a
árvore `snapshots/` renderizada pelo runner sobre as baselines versionadas —
só as que divergiram mudam. **Revise a(s) imagem(ns)**, depois commite e dê
push. (Equivalente manual: baixe o artefato `ui-snapshot-<run_id>` e
commite a sua árvore `snapshots/` sobre `tests/e2e_ui/visual/snapshots/`.)

### Dispatch de workflow (branches sem PR)

GitHub → Actions → **UI Snapshot** → **Run workflow**, defina `ref` para a
sua branch (CLI: `gh workflow run ui-snapshot.yml -f ref=<your-branch>`). Ele
roda com `--update-snapshots` (falha de propósito); o PNG regenerado fica no
artefato `ui-snapshot-<run_id>` para baixar, revisar e commitar. Qualquer
colaborador pode disparar contra um `ref` arbitrário, mas como o PNG é
revisado por humano antes de entrar, um ref não revisado não consegue mudar
a baseline sozinho.

### Comentários de falha

Sempre que a checagem falha (mesmo repositório ou fork),
[`ui-snapshot-fail-comment.yml`](../../../.github/workflows/ui-snapshot-fail-comment.yml)
insere/atualiza um comentário no PR apontando de volta para esses caminhos.
Ele roda como `workflow_run` para poder comentar sem nunca executar código
de PR/fork, o que significa que só ativa depois de mergeado na `main` (não
dispara no seu próprio PR).

## Adicionando o snapshot de uma página nova

Cada página é um teste `@pytest.mark.visual` no seu próprio
`test_<page>_snapshot.py`. O gate, os fluxos de atualização e o artefato já
cobrem todo teste deste diretório, então uma página nova é só um teste + a
sua baseline versionada — nenhuma mudança de workflow.

1. Adicione `test_<page>_snapshot.py`. Pegue `snapshot_page` (viewport fixo +
   paleta clara), `live_server`, `fulfill_json`, `settle_for_snapshot` e
   `assert_snapshot` de [`conftest.py`](conftest.py).
2. Stube via `page.route` **toda** chamada que a página faz, de forma que a
   view seja uma função pura do bundle — sem dado real de backend, sem
   stream ao vivo. Alimente qualquer dado dinâmico a partir de fixtures
   fixas; o teste de chat
   ([`test_chat_snapshot.py`](test_chat_snapshot.py)) é o exemplo
   trabalhado (sessão/itens/agente/health mockados + um stream `[DONE]`).
   Fique atento a não-determinismo: timestamps relativos, streaming,
   shimmers de "trabalhando", ids aleatorizados.
3. Navegue, espere o conteúdo da página terminar de pintar (um seletor
   estável, não um timer), chame `settle_for_snapshot(page)`, depois
   `assert_snapshot(page)`.
4. Gere a baseline na imagem fixada — dê label no PR ou rode
   `regen_baseline_docker.sh` — depois **revise o PNG** e commite.

## Rodando localmente sem Docker (só para debug — nunca commite o resultado)

Você pode exercitar o teste no host para debugar, mas uma baseline
renderizada em qualquer lugar que não seja a imagem fixada não vai bater com
o gate, então **nunca commite um PNG produzido assim** — um `git add -A`
descuidado commitaria uma baseline de renderizador errado e quebraria a CI.
Use o caminho do Docker acima para produzir um PNG commitável.

```bash
uv sync --extra all --extra dev
uv run playwright install --with-deps chromium
cd web && npm ci --legacy-peer-deps && npm run build && cd ..
# A primeira execução sem baseline cria uma (e falha); as seguintes comparam:
uv run pytest tests/e2e_ui/visual -m visual --ui-skip-build
```
