# OmniCraft no Tailscale

O [Tailscale](https://tailscale.com) dá a cada dispositivo da sua rede um
hostname privado estável (`<machine>.ts.net`) e os conecta ponto a ponto via
WireGuard — sem redirecionamento de porta, sem regras de firewall. Isso torna
fácil acessar um servidor rodando no seu notebook a partir do celular, tablet,
ou qualquer outro dispositivo seu.

> [!NOTE]
> Isso não é um deploy na nuvem. O Tailscale é uma camada de rede, não um
> serviço de hospedagem — você continua rodando o servidor você mesmo
> (notebook, VPS, servidor de casa). Se você quer que o servidor continue no
> ar quando o seu notebook fechar, publique numa plataforma de nuvem (veja
> [../README.md](../README.md)) e use o Tailscale só para acesso privado.

## Pré-requisitos

- Tailscale instalado na máquina do servidor e em todo dispositivo cliente.
  Todos logados na mesma conta Tailscale.
- Servidor OmniCraft rodando localmente (ex.: `omnicraft server` ou
  `docker compose up -d` a partir de `deploy/docker/`).

## Acesso só pela tailnet (celular / tablet / notebook remoto)

Exponha o servidor local por HTTPS para todo dispositivo da sua tailnet:

```bash
tailscale serve https / http://localhost:8000
```

O Tailscale emite um certificado TLS para `https://<machine>.ts.net` e faz
proxy do tráfego para `localhost:8000`. Nenhum outro dispositivo na internet
consegue alcançá-lo.

Defina duas variáveis de ambiente no servidor antes de subi-lo:

```dotenv
# Confie na origem do Tailscale para que os handshakes de WebSocket e os
# uploads multipart sejam aceitos a partir do navegador no seu celular/tablet.
OMNICRAFT_WS_ALLOWED_ORIGINS=https://<machine>.ts.net

# URL base pública — usada para montar o prefixo correto do cookie __Host-
# e as URLs de convite / magic-link.
OMNICRAFT_ACCOUNTS_BASE_URL=https://<machine>.ts.net
```

Sem `OMNICRAFT_WS_ALLOWED_ORIGINS` o navegador vai receber o código de
fechamento de WebSocket `4403` e um HTTP 403 *"Forbidden: this endpoint
requires a trusted Origin header"* no chat e nos uploads de arquivo. Sem
`OMNICRAFT_ACCOUNTS_BASE_URL` os cookies de sessão não vão usar o prefixo
`__Host-` e os links de convite vão resolver para o host errado.

**Com o Docker Compose** (`deploy/docker/`), adicione as duas linhas ao seu
`.env`:

```bash
# gere e edite o .env se ainda não tiver feito isso
cp deploy/docker/.env.example deploy/docker/.env

# adicione ao .env:
OMNICRAFT_WS_ALLOWED_ORIGINS=https://<machine>.ts.net
OMNICRAFT_ACCOUNTS_BASE_URL=https://<machine>.ts.net
```

Depois reinicie:

```bash
docker compose up -d
```

Abra `https://<machine>.ts.net` em qualquer dispositivo da sua tailnet.

## Hosts de sandbox na nuvem e o Tailscale Funnel

Provedores de sandbox na nuvem (Modal, Daytona, E2B, …) rodam o processo de
host do OmniCraft *dentro* de um container remoto. Esse host disca **para
fora**, para `server_url`, via WebSocket para receber trabalho — então ele
precisa alcançar o servidor a partir da rede na nuvem do provedor de sandbox,
não só da sua tailnet.

Um servidor atrás de um `tailscale serve` simples só é alcançável a partir da
sua tailnet. O **Tailscale Funnel** resolve isso: ele torna uma porta
específica alcançável a partir da internet pública mantendo o mesmo hostname
`<machine>.ts.net`.

```bash
tailscale funnel 8000
```

Depois aponte a configuração do sandbox para a URL pública do Tailscale:

```yaml
# config.yaml (ou /data/config.yaml no Docker)
sandbox:
  provider: modal          # ou daytona, e2b, …
  server_url: https://<machine>.ts.net
```

> [!IMPORTANT]
> O Funnel torna o servidor alcançável a partir da internet pública, então
> ative a autenticação antes de ligá-lo:
>
> ```dotenv
> OMNICRAFT_AUTH_ENABLED=1
> OMNICRAFT_ACCOUNTS_BASE_URL=https://<machine>.ts.net
> ```
>
> Veja [Autenticação](../README.md#autenticação) para a configuração completa.

## Resumo

| Objetivo | Comando | Alcançável a partir de |
|---|---|---|
| Acesso de dispositivos na sua tailnet | `tailscale serve https / http://localhost:8000` | Só a tailnet |
| Hosts de sandbox na nuvem + tailnet | `tailscale funnel 8000` | Internet pública + tailnet |

## Referência de variáveis de ambiente

| Variável | Finalidade |
|---|---|
| `OMNICRAFT_WS_ALLOWED_ORIGINS` | Allowlist de origens separadas por vírgula. Defina como `https://<machine>.ts.net` para confiar na origem do Tailscale nas rotas de WebSocket e multipart. |
| `OMNICRAFT_ACCOUNTS_BASE_URL` | URL base pública. Usada para a segurança do cookie de sessão (prefixo `__Host-`) e para URLs de convite / magic-link. |
| `OMNICRAFT_AUTH_ENABLED` | `1` para exigir login. Recomendado ao usar o Tailscale Funnel (exposição à internet pública). |
