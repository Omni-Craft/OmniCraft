# OmniCraft no Hugging Face Spaces

> **Alvo de nível demo.** No nível gratuito, o armazenamento do Space é
> **efêmero** — os dados (e o banco SQLite) são reiniciados a cada restart. Bom
> para experimentar, não para manter estado. Para persistência, adicione o
> armazenamento persistente pago da HF, ou aponte `DATABASE_URL` para um
> Postgres externo.

O HF Spaces (Docker SDK) constrói um Dockerfile na raiz do repositório do Space
e o executa. O shim aqui só puxa a imagem pré-construída. **Nenhum banco
externo é necessário** — o servidor roda num arquivo SQLite (um backend de
primeira classe), então um Space de demo é dois arquivos mais dois secrets.

## Configuração

1. Crie um Space **Docker** no Hugging Face.
2. Adicione estes dois arquivos na raiz do repositório do Space:
   - o `Dockerfile` deste diretório (puxa a imagem), e
   - um `README.md` começando com este front-matter (a HF o lê):
     ```yaml
     ---
     title: OmniCraft
     emoji: 🤖
     colorFrom: indigo
     colorTo: blue
     sdk: docker
     app_port: 8000
     ---
     ```
3. Em **Settings -> Variables and secrets** do Space, defina:

   | Nome | Tipo | Valor |
   |---|---|---|
   | `PORT` | variável | `8000` (fixe para que o app e o `app_port` concordem) |
   | `HOST` | variável | `0.0.0.0` |
   | `DATABASE_URL` | variável | `sqlite:////data/artifacts/chat.db` |
   | `OMNICRAFT_ACCOUNTS_COOKIE_SECRET` | secret | `openssl rand -hex 32` (fixe: o disco efêmero derrubaria as sessões a cada restart) |

4. O Space constrói e sobe. A senha do admin está nos **Logs** do Space no
   primeiro boot. A URL base é detectada automaticamente a partir de
   `SPACE_HOST`, então não precisa ser definida manualmente.
5. **Entre pela URL direta** `https://<user>-<space>.hf.space` em sua própria
   aba — não pela prévia embutida da HF. O cookie de sessão é `SameSite=Lax`,
   que os navegadores não enviam dentro do iframe cross-origin da HF, então
   entrar pela visualização embutida faz um loop de volta para `/login`. A URL
   direta é top-level (same-site), então o login se mantém. Deixe o Space
   **Public** para que a URL direta não fique bloqueada.

## Quer persistência / multiusuário depois?

O SQLite num Space gratuito é efêmero (reinicia a cada restart). Para dados
que sobrevivem, troque `DATABASE_URL` por um Postgres externo **seu** — o mais
rápido é o Neon:

1. Vá a [pg.new](https://pg.new) e crie um Postgres gratuito. **Entre com uma
   conta para mantê-lo** — um banco instantâneo sem dono é descartável e
   expira.
2. Copie a string de conexão e defina-a como o secret `DATABASE_URL` do Space
   (substituindo o valor do SQLite). O entrypoint normaliza `postgres://`
   automaticamente; tanto a string de conexão com pool quanto a direta
   funcionam.

Isso faz os dados sobreviverem a restarts e suporta mais de uma instância.
Note que o **primeiro boot leva ~1 minuto** enquanto as migrações rodam contra
o banco remoto (os boots seguintes são rápidos), então não se assuste se o
Space ficar em "Building / Starting" por um tempo.
