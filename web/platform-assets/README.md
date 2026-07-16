# Platform Assets

Assets nativos de plataforma compartilhados para wrappers em volta da web UI
do OmniCraft.

- `AppIcon.icon` é a fonte da verdade do Apple Icon Composer para o ícone do
  app. O projeto iOS referencia ele diretamente. O Electron consome
  artefatos gerados em `electron/icons/` (`Assets.car`, `icon.icns`,
  `icon.png` e `icon.ico`) para que o empacotamento não precise do Xcode 26.
- `logos/` contém os SVGs de logo da tela de configuração. O Electron os
  carrega de `platform-assets` em tempo de execução; o iOS os simlinka para
  dentro do seu catálogo de assets para que a tela de configuração em
  SwiftUI use as mesmas fontes.
