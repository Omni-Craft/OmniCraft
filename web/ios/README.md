# OmniCraft iOS

Um shell fino em SwiftUI/WKWebView para o OmniCraft. Como o app Electron,
esse target carrega a web UI servida pelo servidor em vez de embarcar uma
cópia duplicada da SPA.

## Desenvolvimento

Abra `OmniCraft.xcodeproj` no Xcode 16 ou mais novo e rode o scheme
`OmniCraft` num simulador iOS 18.

Builds de debug permitem conteúdo web `http://` para desenvolvimento local
ao ativar `NSAllowsArbitraryLoadsInWebContent`. Builds de release mantêm os
padrões do App Transport Security e exigem que servidores remotos usem
`https://`.

## Escopo

A primeira versão fornece chrome nativo de configuração, servidores
recentes, carregamento de WKWebView, notificações locais em primeiro plano,
atualizações de badge do app e roteamento de toque de notificação de volta
para a SPA. Ela não implementa APNs, polling em segundo plano, ou
comportamento de proxy/CORS para localhost.
