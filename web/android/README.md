# OmniCraft Android

Um shell fino em Kotlin/`WebView` para o OmniCraft. Como o app Electron e o
shell iOS (`web/ios`), esse target carrega a web UI servida pelo servidor em
vez de embarcar uma cópia duplicada da SPA. É um _shell_ nativo, não uma
reescrita.

## Desenvolvimento

Abra `web/android` no Android Studio (Ladybug / AGP 8.6+) e rode a
configuração `app` num emulador API 34/35. Exige JDK 17 e o Android SDK
(`compileSdk 35`, `minSdk 28`).

Builds de debug permitem cleartext (`http://`) para localhost e hosts de
faixa privada via `res/xml/network_security_config.xml` para desenvolvimento
local; builds de release mantêm o padrão da plataforma (só HTTPS), espelhando
a postura debug-only do `NSAllowsArbitraryLoadsInWebContent` do iOS.

## Como se relaciona com o bundle web

O mesmo bundle `web/` roda numa aba de navegador, no shell Electron, no
shell WKWebView do iOS e nesse WebView do Android. A detecção é baseada em
feature em tempo de execução via `window.omnicraftNative` — veja
`web/src/lib/nativeBridge.ts`. Esse shell injeta esse objeto com
`kind: "android"`; a camada web não precisa de branching por feature além do
discriminador `kind` (`isAndroidShell()`).

O transporte web→nativo é um canal `WebViewCompat.addWebMessageListener`
(`OmniCraftBridgeListener`) **com allowlist de origem fixada no servidor
pinado** e gateado em `isMainFrame`, em vez de `addJavascriptInterface`.
Isso é o equivalente estrutural da checagem de origem-de-frame +
`isMainFrame` da bridge do iOS: o objeto de transporte nunca é entregue a um
iframe agent-HTML sandboxado / cross-origin, então um artefato injetado não
consegue alcançar a superfície nativa.

## Escopo (primeira versão)

Fornece chrome nativo de configuração (entrada de servidor + servidores
recentes via `ConnectActivity`), carregamento de `WebView`, notificações
locais em primeiro plano com roteamento de toque de volta para a SPA, um
badge de app best-effort, encanamento de insets edge-to-edge (insets
medidos injetados como `--omnicraft-android-safe-area-*`, consumidos pelo
sistema de inset web), tratamento correto de back de sistema /
predictive-back, downloads de arquivo — incluindo exports `blob:` / `data:`
via uma bridge fetch→base64→MediaStore, que fecha o
omnicraft-ai/omnicraft#969 (o shell iOS descarta esses exports), **uploads** de
arquivo (`<input type=file>` via `WebChromeClient.onShowFileChooser`), e
captura de **microfone** para entrada de voz (`onPermissionRequest`,
concedida só à origem pinada, com uma requisição em tempo de execução de
`RECORD_AUDIO`).

### Deixado deliberadamente para os fallbacks in-page da web

Esses são chrome nativo exclusivo do iOS; a SPA já renderiza os
equivalentes dela quando os métodos da bridge estão ausentes, então o shell
Android os omite por enquanto:

- **Drawer de sidebar por edge-swipe interativo.** Não é portável: no
  Android 10+ o gesto de sistema back é dono das duas bordas de tela, e
  `View.setSystemGestureExclusionRects()` não se aplica a ele. A sidebar
  abre pelo hambúrguer in-page, exatamente como numa aba de navegador.
- **Trocador flutuante de servidor nativo** e **barra de Chat/Terminal.**
  Renderizados in-page pela SPA.

### Lacunas de paridade conhecidas

- **Contagem no badge do app.** O Android não tem uma API numérica de badge
  universal. Usamos `NotificationCompat.setNumber()` (exibido por alguns
  launchers; AOSP/Pixel mostra só um ponto) e tratamos o ponto de
  notificação como a superfície garantida. `setBadgeCount(0)` é um no-op —
  não cancelamos notificações para limpar um badge.

## Distribuição

O Gradle monta um APK/AAB de release; o `fastlane` (Android) automatiza a
assinatura e o upload. A Google Play restringe apps "WebView de um site",
então o canal inicial é APK direto / F-Droid; um cliente de servidor
configurado pelo usuário é um caso mais forte para a Play, mas a análise é
imprevisível para essa categoria.

> Status: builda limpo — `gradlew :app:assembleDebug :app:lintDebug` produz
> um APK de debug com 0 erros de lint (JDK 17, Gradle 8.9 wrapper,
> `compileSdk 35`). Implementação para omnicraft-ai/omnicraft#1604; ainda
> não exercitado num dispositivo (sem teste de runtime/instrumentado aqui),
> então trate o comportamento em dispositivo como não verificado.
