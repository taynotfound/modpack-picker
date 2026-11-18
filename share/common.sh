# Shared helpers for the modpack-picker and minecraft-handheld scripts.
# Sourced, not run.

APP=org.prismlauncher.PrismLauncher
PRISM_DATA="$HOME/.var/app/$APP/data/PrismLauncher"
INST_DIR="$PRISM_DATA/instances"
CONF_DIR="$HOME/.config/minecraft-handheld"
STARRED="$CONF_DIR/instance"
SETTINGS="$CONF_DIR/settings.json"
PICKER_DIR="${MODPACK_PICKER_DIR:-$HOME/.local/share/modpack-picker}"
PICKER_BIN="${MODPACK_PICKER_BIN:-$HOME/.local/bin/modpack-picker}"

# setting KEY DEFAULT: read one value from settings.json
setting() {
    local v
    v=$(python3 -I -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except Exception: pass' "$SETTINGS" "$1" 2>/dev/null)
    echo "${v:-$2}"
}

# Prints one instance folder name per line.
list_instances() {
    [ -d "$INST_DIR" ] || return 0
    find "$INST_DIR" -mindepth 1 -maxdepth 1 -type d ! -name '.*' \
        -exec test -f {}/instance.cfg \; -printf '%f\n'
}

valid_instance() {
    [ -n "$1" ] && [ -f "$INST_DIR/$1/instance.cfg" ]
}

starred_instance() {
    local s
    s=$(head -n1 "$STARRED" 2>/dev/null)
    valid_instance "$s" && echo "$s"
}

# The instance with the newest lastLaunchTime (nothing if none was played yet).
last_played() {
    local best="" bestt=0 i t
    while IFS= read -r i; do
        t=$(sed -n 's/^lastLaunchTime=\([0-9]*\).*/\1/p' "$INST_DIR/$i/instance.cfg" | head -n1)
        if [ -n "$t" ] && [ "$t" -gt "$bestt" ]; then best=$i; bestt=$t; fi
    done < <(list_instances)
    echo "$best"
}

# Make Prism hide while the game runs and quit when it closes, so Game Mode
# drops you back into Steam (or the game list) instead of a desktop window.
# Only fills in keys you have not set yourself.
prism_handheld_defaults() {
    local cfg="$PRISM_DATA/prismlauncher.cfg"
    [ -f "$cfg" ] || return 0
    # Prism rewrites its config when it exits, so leave it alone while running.
    flatpak ps --columns=application 2>/dev/null | grep -qx "$APP" && return 0
    grep -q '^\[General\]' "$cfg" || return 0
    local k
    for k in CloseAfterLaunch QuitAfterGameStop; do
        grep -q "^$k=" "$cfg" || sed -i "/^\[General\]/a $k=true" "$cfg"
    done
}

# Start an instance and wait until Prism exits.
launch_game() {
    prism_handheld_defaults
    flatpak run "$APP" --launch "$1"
}
