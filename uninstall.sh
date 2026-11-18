#!/bin/bash
# Removes Modpack Picker. Your Prism Launcher games and worlds are not touched.
set -eu
rm -f "$HOME/.local/bin/modpack-picker" "$HOME/.local/bin/minecraft-handheld" \
      "$HOME/.local/share/applications/modpack-picker.desktop" \
      "$HOME/.local/share/applications/minecraft-handheld.desktop"
rm -rf "$HOME/.local/share/modpack-picker"
echo "Removed. Settings are kept in ~/.config/minecraft-handheld (delete it if you like)."
