# OmniCraft no BoxLite

O [BoxLite](https://github.com/boxlite-ai/boxlite) é um runtime embarcável de
micro-VM + OCI ("SQLite para sandboxing"). Ele roda cada host do OmniCraft
dentro da sua própria VM leve (com kernel próprio — KVM no Linux,
Hypervisor.framework no macOS) inicializada a partir de uma imagem OCI padrão.

O provider boxlite é **só gerenciado pelo servidor**: o servidor provisiona um
box automaticamente quando uma sessão é criada com `"host_type":
"managed"`, sobe o `omnicraft host` dentro dele, e o remove quando a
sessão é apagada. (Ainda não existe um bootstrap de CLI `omnicraft sandbox
create` para o boxlite — veja [Limitações](#limitações).)

Um único provider `boxlite` cobre **os dois** destinos de runtime, escolhidos
pela configuração:

- **Local** (padrão — sem bloco `cloud:`): o BoxLite fica embarcado no
  processo do servidor OmniCraft. **Sem daemon, sem `boxlite serve`, sem
  root.** Os boxes são micro-VMs no próprio host do servidor, então esse
  host precisa de virtualização por hardware. O primeiro runner local,
  isolado por hardware e persistente — sem conta na nuvem necessária.
- **Cloud** (um bloco `cloud:` com `endpoint`): um cliente REST fino para um
  pool remoto de `boxlite serve`. Os boxes rodam no pool; o servidor os
  alcança por HTTP. Mesmo papel dos providers Modal / Daytona, self-hosted.

Os dois modos são configurados por sub-blocos mutuamente exclusivos `local:` /
`cloud:` (veja [Configuração do servidor](#configuração-do-servidor)).

Os boxes inicializam a partir da imagem oficial pré-pronta do host, então o
startup leva segundos assim que a imagem já está em cache localmente (o
primeiro boot a partir de uma imagem faz o pull dela, o que pode levar alguns
minutos).

## Pré-requisitos

```bash
pip install 'omnicraft[boxlite]'   # instala o extra do SDK do boxlite
```

O **modo local** precisa, além disso, de virtualização por hardware no
*host do servidor*:

- **Linux:** KVM habilitado e acessível — `/dev/kvm` precisa existir e o
  usuário do servidor precisa estar no grupo `kvm`.
- **macOS (Apple Silicon):** Hypervisor.framework, sempre disponível.

O **modo cloud** precisa de um endpoint `boxlite serve` alcançável; o host
do servidor não precisa de virtualização.

## Configuração do servidor

Adicione um bloco `sandbox:` na configuração do seu servidor
(`omnicraft server -c …` / `OMNICRAFT_CONFIG` / `<data_dir>/config.yaml`).

### Micro-VMs locais (sem conta na nuvem)

```yaml
sandbox:
  provider: boxlite
  server_url: https://omnicraft.example.com   # o host dentro do box disca de volta para cá
```

`provider` + `server_url` já é uma configuração completa: a imagem assume por
padrão a imagem oficial pré-pronta do host e os boxes rodam localmente.

### Cloud (pool remoto `boxlite serve`)

```yaml
sandbox:
  provider: boxlite
  server_url: https://omnicraft.example.com
  boxlite:
    image: docker.io/me/omnicraft-host:latest     # opcional, compartilhado; padrão: o oficial
    env: [OPENAI_API_KEY, GIT_TOKEN]             # opcional, compartilhado; NOMES de variável de ambiente do SERVIDOR
    cloud:
      endpoint: https://boxlite.example.com:8100 # seleciona o modo CLOUD
```

`local:` e `cloud:` são **mutuamente exclusivos** — uma sessão roda em
exatamente um modo. As credenciais do provider **não** ficam neste arquivo
(12-factor): no modo cloud a chave de API é lida de `BOXLITE_API_KEY` no
ambiente do servidor.

### Personalização do runtime local (diretório de dados, imagem privada do host)

O modo local embarca o runtime do boxlite, então você pode apontá-lo para um
diretório de dados específico e dar a ele credenciais para puxar uma imagem
**privada** do host (o análogo local dos secrets de registry dos providers de
cloud):

```yaml
sandbox:
  provider: boxlite
  server_url: https://omnicraft.example.com
  boxlite:
    image: ghcr.io/acme/omnicraft-host:latest   # compartilhado
    local:                           # bloco do modo LOCAL (mutuamente exclusivo com `cloud`)
      home_dir: /data/boxlite        # estado de runtime + cache de imagem (padrão ~/.boxlite)
      registry:
        host: ghcr.io
        username_env: GHCR_USER      # NOME de uma variável de ambiente do servidor (não o valor)
        password_env: GHCR_PAT
        # token_env: GHCR_TOKEN      # alternativa com bearer token
        # transport: https           # ou "http"
        # skip_verify: false
```

O bloco `local:` vale só para o modo local e é mutuamente exclusivo com
`cloud:`. Quando `local:` é omitido (ou vazio) o launcher usa o runtime padrão
`Boxlite.default()`, sem configuração. As credenciais de registry são lidas
das variáveis de ambiente do servidor indicadas por nome, no momento do
provisionamento — os valores nunca ficam no arquivo de configuração.

> **Segurança:** `transport: https` (o padrão) e `skip_verify: false` mantêm
> o pull do registry criptografado e com o certificado verificado.
> `transport: http` manda as credenciais do pull em **texto puro**, e
> `skip_verify: true` desliga a verificação de TLS — use os dois só numa rede
> local confiável. Da mesma forma, um `endpoint` de cloud com esquema
> `http://` manda o `BOXLITE_API_KEY` em texto puro; prefira `https://`.

### Variáveis de ambiente

| Variável | Finalidade |
|----------|---------|
| `BOXLITE_API_KEY` | Chave de API para o `boxlite serve` remoto (só no modo cloud). |
| `OMNICRAFT_BOXLITE_HOST_IMAGE` | Sobrescreve a imagem do host (alternativa a `sandbox.boxlite.image`). |
| `OMNICRAFT_BOXLITE_SANDBOX_ENV` | Nomes de variável de ambiente do SERVIDOR, separados por vírgula, para injetar nos boxes (alternativa a `sandbox.boxlite.env`). |

Os nomes de `env` são resolvidos para os seus valores a partir do **próprio
ambiente do servidor** no momento do provisionamento — normalmente as
credenciais de LLM do harness (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, URLs
base de gateway) e o `GIT_TOKEN` que o host dentro do box repassa para os
runners. Só os nomes, então os valores de segredo nunca ficam no arquivo de
configuração.

## Como funciona

1. O servidor provisiona um box a partir da imagem pré-pronta do host
   (`runtime.create(BoxOptions(image=…, auto_remove=False))`). Os boxes são
   persistentes — a maquinaria de sessão gerenciada é dona do teardown.
2. A rede assume egress total por padrão, para que o host dentro do box
   consiga alcançar `server_url`.
3. O servidor roda o `omnicraft host` dentro do box (via `box.exec`) com um
   token de lançamento de uso único no ambiente dele; o host disca de volta
   por um túnel WebSocket e se registra. A partir daí a sessão anda na mesma
   maquinaria de host/runner que todo host do OmniCraft usa — o runner, as
   ferramentas e o shell do agente rodam todos dentro do box.
4. Se o sandbox morrer (uma queda, ou você rodar `boxlite rm` nele), a
   identidade durável do host sobrevive e a próxima mensagem relança uma nova
   geração de box.

Inspecione os boxes em execução pela CLI (`boxlite list`, `boxlite logs
<id>`); o host dentro do box escreve logs em `/tmp/omnicraft-host.log`.

## Limitações

- **Só gerenciado.** O bootstrap de CLI `omnicraft sandbox create` /
  `connect` (envio da wheel local + App OAuth dentro do sandbox) não está
  implementado para o boxlite. Use o fluxo gerenciado pelo servidor acima.
  (Adicionar o bootstrap de CLI depois é direto — o `Box.copy_into`
  assíncrono já suporta envio de arquivo; o wrapper síncrono do SDK não
  suporta, e é por isso que o launcher usa a API assíncrona.)
- **Política de rede.** Os boxes ganham egress total de saída por padrão. Se
  o seu deploy precisa de uma allowlist, isso é um follow-up no campo de
  rede do `BoxOptions`.
