# Política de Segurança

Para relatar uma vulnerabilidade de segurança, use os
[GitHub private security advisories](https://github.com/Omni-Craft/OmniCraft/security/advisories/new).

Por favor, não abra uma issue pública para problemas de segurança, e não inclua
credenciais ativas, tokens ou dados de clientes em nenhum relato.

## Gate de segurança para PRs de contribuidores

O CI para PRs não confiáveis fica retido atrás de um scan de segurança
determinístico, de modo que código não confiável não é feito checkout, buildado
ou executado em nossos runners — e o cache das Actions não é tocado — até que o
diff tenha sido verificado. Isso é dividido em duas partes para que o trabalho
de scan aconteça apenas **uma vez por PR**:

- **`.github/workflows/security-scan.yml`** — executa o scan determinístico uma
  vez em `pull_request` e produz o check `Security Scan`.
- **`.github/workflows/security-gate.yml`** — um poller reutilizável executado
  como o primeiro job (`gate`) de todo workflow de CI (`ci`, `lint`, `e2e`,
  `e2e-ui`, testes web); os jobs reais declaram `needs: gate`. Ele não refaz o
  scan — para um PR não confiável, ele aguarda o check `Security Scan` e espelha
  seu resultado (falha → os jobs de CI dependentes são pulados); autores
  confiáveis e eventos que não são PR prosseguem imediatamente.

Por nível de confiança (`author_association` do GitHub):

- **Confiável** (`OWNER` / `MEMBER` / `COLLABORATOR`) e todos os eventos que não
  são PR (push, schedule, dispatch): o gate passa instantaneamente, sem scan.
- **Contribuidor recorrente** (`CONTRIBUTOR`): o gate executa o scan; um
  resultado limpo permite que o CI prossiga automaticamente, um achado bloqueia
  todo o CI.
- **Contribuidor de primeira vez**: a configuração nativa do GitHub *"require
  approval to run fork pull request workflows"* já retém todo workflow até que
  um mantenedor clique em **Approve and run**; após a aprovação, o scan do gate
  ainda se aplica.

O scan inspeciona o diff do PR em busca de segredos commitados, padrões de
exfiltração de segredos (uma fonte de credencial com nome de segredo mais um
sink de rede no mesmo arquivo, um dump de `os.environ`, um decode-then-exec, ou
uma reverse shell), alterações em configuração privilegiada do repositório
(workflows de CI, `.github/MAINTAINER`, `CODEOWNERS`, `.github/scripts`), uso
indevido de workflow de CI (`pull_request_target` combinado com checkout do
head do PR, actions não fixadas/unpinned) e padrões conhecidos de execução de
código / ofuscação (semgrep, ruleset local). Ele apenas analisa o diff
*estaticamente* e roda **sem segredos** em PRs de fork, e o próprio scanner
sempre roda a partir de `main`, de modo que um PR não pode enfraquecer seu
próprio scan.

Este **não** é um check obrigatório para merge: ele controla o CI, não o botão
de merge diretamente. Quando em vigor, o merge permanece bloqueado
transitivamente (os checks de pytest/e2e pulados são obrigatórios) e o
`Maintainer Approval` permanece o gate definitivo.

Ele é **bloqueante**: um achado reprova o check `Security Scan`, os pollers
espelham essa falha, e os jobs de CI dependentes são pulados. Os detectores
rodam em modo fail-fast, então um PR limpo precisa passar em todos eles.

### Substituição pelo mantenedor

Um mantenedor pode dispensar o scan em um PR específico com a label
**`skip-security-scan`** (mesma convenção de `skip-e2e-ui-test`). A dispensa só
é aceita quando é *efetiva por um mantenedor*: a label está presente **e** o
autor do PR é um mantenedor, ou a revisão decisiva mais recente de um
mantenedor é `APPROVED`. A label sozinha não faz nada — aplicar labels exige
acesso de triagem, e a checagem adicional do mantenedor é uma defesa em
profundidade — então um contribuidor de fork não pode dispensar o scan por
conta própria. A label e o estado da revisão são lidos da API, e a decisão roda
a partir de `should-scan.sh` em `main`, de modo que um PR não pode editar a
lógica de dispensa.

Para usar: um mantenedor revisa/aprova o PR e aplica `skip-security-scan`; o
check `Security Scan` roda novamente e passa, então os workflows de CI
bloqueados são reexecutados (ou o contribuidor faz push) para que seus jobs de
gate vejam o scan agora verde. A dispensa permanece efetiva através de pushes
subsequentes enquanto a aprovação do mantenedor se mantiver — remova a label
(ou descarte a aprovação) para reabilitar o scan.
