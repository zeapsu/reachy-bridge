#!/usr/bin/env bash
# Install or update reachy-bridge from this checkout as user services (#128).
# Safe to re-run after a pull.
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
# It changes nothing on the device until the robot is confirmed off and the
# locked reachy-mini has passed the runtime's --check in a throwaway venv.
# The daemon is stopped while its venv is replaced, then started again (the
# robot starts off).
#
# Needs uv, cargo and bun until releases ship prebuilt binaries (#131).
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DEST=$HOME/.local/share/reachy-bridge
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
BIN_DIR=$HOME/.local/bin
CLI=$REPO/deploy/reachy-bridge
DAEMON=reachy-bridge.service
CONTROLLER=reachy-bridge-controller.service

step() { printf '\n== %s\n' "$*"; }
die() { echo "install: $*" >&2; exit 1; }

for tool in uv cargo bun systemctl curl; do
    command -v "$tool" >/dev/null || die "$tool is not on PATH"
done
uv_dir=$(dirname "$(command -v uv)")
# The daemon unit's PATH points here for app installs; a version-specific
# directory would break when that version is pruned.
[[ $uv_dir != */installs/* ]] || die "uv resolves to $uv_dir; put a stable uv on PATH (e.g. ~/.local/bin/uv)"

require_robot_off() {
    systemctl --user is-active --quiet "$DAEMON" || return 0
    local power
    power=$("$CLI" robot-power)
    [[ $power == off ]] ||
        die "the robot isn't confirmed off (${power}); turn it off from the dashboard and re-run. Nothing was changed."
}

step "Preflight"
require_robot_off
if [[ -d $UNIT_DIR/$DAEMON.d || -d $UNIT_DIR/$CONTROLLER.d ]]; then
    echo "warning: drop-ins in $UNIT_DIR/*.service.d override the installed units"
fi
staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
# The lock decides the daemon version. Check it against the runtime's
# upstream seam before touching the live venv.
UV_PROJECT_ENVIRONMENT="$staging/venv" uv sync --project "$REPO" --frozen --no-dev \
    --no-editable --package reachy-bridge-runtime --quiet
"$staging/venv/bin/reachy-bridge-runtime" --check ||
    die "the locked reachy-mini doesn't match the runtime's upstream seam (see above). Nothing was changed."

step "Build the controller"
(cd "$REPO/web" && bun install --frozen-lockfile && bun run build)
(cd "$REPO/controller" && cargo build --release --locked)

step "Install"
# The build takes minutes; the robot may have been switched on meanwhile.
require_robot_off
systemctl --user stop "$DAEMON" 2>/dev/null || true
mkdir -p "$DEST/bin"
# --no-editable, so the service doesn't depend on this checkout.
UV_PROJECT_ENVIRONMENT="$DEST/.venv" uv sync --project "$REPO" --frozen --no-dev \
    --no-editable --package reachy-bridge-runtime
install -m 755 "$REPO/controller/target/release/reachy-controller" "$DEST/bin/reachy-controller.new"
mv -f "$DEST/bin/reachy-controller.new" "$DEST/bin/reachy-controller"
install -D -m 755 "$CLI" "$BIN_DIR/reachy-bridge"
mkdir -p "$UNIT_DIR"
# rm first: never write through a symlinked unit file to its target.
rm -f "$UNIT_DIR/$DAEMON" "$UNIT_DIR/$CONTROLLER"
sed "s|@UV_DIR@|$uv_dir|" "$REPO/deploy/systemd/$DAEMON" >"$UNIT_DIR/$DAEMON"
install -m 644 "$REPO/deploy/systemd/$CONTROLLER" "$UNIT_DIR/$CONTROLLER"
systemctl --user daemon-reload
systemctl --user enable "$DAEMON" "$CONTROLLER"

step "Start"
systemctl --user start "$DAEMON"
systemctl --user restart "$CONTROLLER"
echo "daemon started (robot off); controller on 127.0.0.1:7861"

if [[ $(loginctl show-user "$USER" -p Linger --value 2>/dev/null) != yes ]]; then
    echo
    echo "Note: user services start at boot only with lingering enabled:"
    echo "  sudo loginctl enable-linger $USER"
fi
echo
echo "Done. Publish on the tailnet with: reachy-bridge serve"
