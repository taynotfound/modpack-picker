#!/bin/bash
# Installs Modpack Picker for the current user. Safe to run again to update.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
SHARE="$HOME/.local/share/modpack-picker"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"

need() { command -v "$1" >/dev/null || { echo "Missing: $1. $2" >&2; exit 1; }; }
need python3 "Install Python 3.8 or newer."
need flatpak "Prism Launcher is used through Flatpak."

mkdir -p "$SHARE/fonts" "$BIN" "$APPS"
install -m644 "$SRC/share/server.py" "$SRC/share/index.html" "$SRC/share/common.sh" "$SHARE/"
install -m644 "$SRC/share/fonts/pixel.ttf" "$SRC/share/fonts/OFL.txt" "$SHARE/fonts/"
install -m755 "$SRC/bin/modpack-picker" "$SRC/bin/minecraft-handheld" "$BIN/"

cat > "$APPS/modpack-picker.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Modpack Picker
Comment=Install Minecraft modpacks or build your own with a controller
Exec=$BIN/modpack-picker
Icon=org.prismlauncher.PrismLauncher
Categories=Game;
EOF
cat > "$APPS/minecraft-handheld.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Minecraft
Comment=Start Minecraft, or choose a game with your controller
Exec=$BIN/minecraft-handheld
Icon=org.prismlauncher.PrismLauncher
Categories=Game;
EOF

echo "Installed:"
echo "  $BIN/modpack-picker      (Modpack Picker in your app menu)"
echo "  $BIN/minecraft-handheld  (Minecraft in your app menu)"

if ! flatpak info org.prismlauncher.PrismLauncher >/dev/null 2>&1; then
    echo
    echo "Prism Launcher isn't installed yet. Get it with:"
    echo "  flatpak install flathub org.prismlauncher.PrismLauncher"
    echo "Then open it once on the desktop and sign in with your Microsoft account."
fi
if ! flatpak info org.chromium.Chromium >/dev/null 2>&1 && \
   ! flatpak info org.anatase.Browser >/dev/null 2>&1 && \
   ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null && \
   ! command -v google-chrome >/dev/null && ! command -v google-chrome-stable >/dev/null; then
    echo
    echo "No Chromium-based browser found for the gamepad UI. Get one with:"
    echo "  flatpak install flathub org.chromium.Chromium"
fi
echo
echo "To use it in Steam Game Mode: add 'Minecraft' (and optionally 'Modpack Picker')"
echo "as Non-Steam Games from desktop mode."
