#!/usr/bin/env bash
# Monta OmniCraftNotch.app a partir do binário do SwiftPM.
#
# O SwiftPM produz um executável solto, e um executável solto não é um app:
# o macOS não o trata como agente de UI, então ele ganharia ícone no Dock e
# roubaria foco. O bundle existe para carregar o Info.plist que diz o
# contrário (LSUIElement).
#
# Uso: scripts/make-app.sh [debug|release]   (padrão: release)
set -euo pipefail

CONFIG="${1:-release}"
cd "$(dirname "$0")/.."

swift build -c "$CONFIG"
BIN="$(swift build -c "$CONFIG" --show-bin-path)/OmniCraftNotch"
[ -x "$BIN" ] || { echo "binário não encontrado: $BIN" >&2; exit 1; }

APP=".build/OmniCraftNotch.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

cp "$BIN" "$APP/Contents/MacOS/OmniCraftNotch"

# Resource bundles of dependencies (the mascot sprites live in
# OmniCraftPets_OmniCraftPets.bundle). SwiftPM leaves them beside the binary
# and Bundle.module looks for them beside the executable, so they have to ride
# along — copying only the binary ships an island with no pet.
BIN_DIR="$(dirname "$BIN")"
for bundle in "$BIN_DIR"/*.bundle; do
  [ -e "$bundle" ] && cp -R "$bundle" "$APP/Contents/MacOS/"
done

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>OmniCraft Ilha</string>
    <key>CFBundleDisplayName</key>
    <string>OmniCraft Ilha</string>
    <key>CFBundleIdentifier</key>
    <string>app.omnicraft.notch</string>
    <key>CFBundleExecutable</key>
    <string>OmniCraftNotch</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>0.1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <!-- Agente de UI: sem ícone no Dock, sem menu próprio, nunca rouba foco. -->
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

# Assinatura ad-hoc: sem isto o macOS mata o app em Apple Silicon.
codesign --force --sign - "$APP" 2>/dev/null || echo "aviso: codesign falhou (app pode não abrir)" >&2

echo "$APP"
