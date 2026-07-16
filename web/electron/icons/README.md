# Ícones do app

- `../../platform-assets/AppIcon.icon` — fonte da verdade do ícone da
  plataforma Apple: um bundle do Apple Icon Composer (arte em camadas +
  fundo em gradiente), compartilhado por Electron e iOS.
- `Assets.car` + `icon.icns` — compilados a partir de `AppIcon.icon` pelo
  `actool` (versionados para que os builds não precisem do Xcode 26+).
  `Assets.car` dá o ícone dinâmico nativo no macOS 26+ (liquid glass,
  claro/escuro/tintado); `icon.icns` é o fallback estático usado por macOS
  mais antigos e como o `mac.icon` do electron-builder. O hook
  `build/afterPack.js` copia `Assets.car` para dentro do bundle do app, e
  `CFBundleIconName=AppIcon` (em `mac.extendInfo`) diz ao macOS para
  procurar por ele. O electron-builder ainda não tem suporte nativo a
  `.icon` — veja
  https://github.com/electron-userland/electron-builder/issues/9254.
- `icon.ico` / `icon.png` — ícones de Windows / Linux.

## Regenerando depois de editar o AppIcon.icon

Exige Xcode 26+ (suporte a `.icon` do Icon Composer no actool):

```bash
cd web/electron/icons
TMP=$(mktemp -d)
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcrun actool ../../platform-assets/AppIcon.icon \
  --compile "$TMP" --platform macosx --minimum-deployment-target 11.0 \
  --app-icon AppIcon --output-partial-info-plist "$TMP/partial.plist"
cp "$TMP/Assets.car" Assets.car
cp "$TMP/AppIcon.icns" icon.icns
rm -rf "$TMP"
```
