#!/usr/bin/env bash
# Install or update reachy-bridge from this checkout as user services (#128).
# Safe to re-run: it rebuilds what changed and never restarts the daemon while
# the robot is on.
#
#   deploy/install.sh
#
# Lays down, under ~/.local/share/reachy-bridge:
#   .venv/           the runtime and Pollen's daemon (reachy-mini at the locked version)
#   apps_venv/       community apps, managed by the daemon (left untouched here)
#   bin/reachy-controller
# and the reachy-bridge and reachy-bridge-controller user units, plus the
# reachy-bridge command in ~/.local/bin.
#
# Needs uv, cargo and bun until releases ship prebuilt binaries (#131).
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DEST=${REACHY_BRIDGE_HOME:-$HOME/.local/share/reachy-bridge}
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
BIN_DIR=$HOME/.local/bin
DAEMON_URL=http://127.0.0.1:8000

step() { printf '\n== %s\n' "$*"; }
die() { echo "install: $*" >&2; exit 1; }

for tool in uv cargo bun systemctl curl; do
    command -v "$tool" >/dev/null || die "$tool is not on PATH"
done
[[ $DEST == "$HOME/.local/share/reachy-bridge" ]] ||
    die "the units expect ~/.local/share/reachy-bridge; unset REACHY_BRIDGE_HOME"

step "Runtime venv ($DEST/.venv)"
mkdir -p "$DEST"
# The lock decides the daemon version; --no-editable so the service doesn't
# depend on this checkout staying put.
UV_PROJECT_ENVIRONMENT="$DEST/.venv" uv sync --project "$REPO" --frozen --no-dev \
    --no-editable --package reachy-bridge-runtime
"$DEST/.venv/bin/reachy-bridge-runtime" --check >/dev/null ||
    die "the installed reachy-mini doesn't match the runtime's upstream seam (run: $DEST/.venv/bin/reachy-bridge-runtime --check)"

step "Controller ($DEST/bin/reachy-controller)"
(cd "$REPO/web" && bun install --frozen-lockfile && bun run build)
(cd "$REPO/controller" && cargo build --release --locked)
install -D -m 755 "$REPO/controller/target/release/reachy-controller" "$DEST/bin/reachy-controller.new"
mv -f "$DEST/bin/reachy-controller.new" "$DEST/bin/reachy-controller"

step "Command ($BIN_DIR/reachy-bridge)"
install -D -m 755 "$REPO/deploy/reachy-bridge" "$BIN_DIR/reachy-bridge"

step "User units ($UNIT_DIR)"
mkdir -p "$UNIT_DIR"
uv_dir=$(dirname "$(command -v uv)")
sed "s|@UV_DIR@|$uv_dir|" "$REPO/deploy/systemd/reachy-bridge.service" >"$UNIT_DIR/reachy-bridge.service"
install -m 644 "$REPO/deploy/systemd/reachy-bridge-controller.service" "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable reachy-bridge.service reachy-bridge-controller.service

step "Services"
if systemctl --user is-active --quiet reachy-bridge.service; then
    status=$(curl -s -m 2 "$DAEMON_URL/api/daemon/status" || true)
    if [[ -z $status || $status == *'"backend_status":null'* ]]; then
        systemctl --user restart reachy-bridge.service
        echo "daemon restarted"
    else
        echo "daemon NOT restarted: the robot is on. Turn it off, then run: reachy-bridge restart daemon"
    fi
else
    systemctl --user start reachy-bridge.service
    echo "daemon started (robot off)"
fi
systemctl --user restart reachy-bridge-controller.service
echo "controller restarted on 127.0.0.1:7861"

if [[ $(loginctl show-user "$USER" -p Linger --value 2>/dev/null) != yes ]]; then
    echo
    echo "Note: user services start at boot only with lingering enabled:"
    echo "  sudo loginctl enable-linger $USER"
fi
echo
echo "Done. Publish on the tailnet with: reachy-bridge serve"
