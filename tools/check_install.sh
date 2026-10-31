#!/usr/bin/env bash
# What deploy/install.sh should leave behind, checked on the machine it ran
# on (CI's install job, #201). With --gone: what the uninstaller should.
#
#   bash tools/check_install.sh [--gone]
set -euo pipefail

DEST=$HOME/.local/share/reachy-bridge
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
UNITS=(reachy-bridge.service reachy-bridge-controller.service reachy-bridge-sim.service
    reachy-bridge-audio.service reachy-bridge-mic.service)
failed=0
fail() { echo "FAIL: $*"; failed=1; }
ok() { echo "ok: $*"; }

# Waits up to a minute for URL to answer 200.
answers() {
    local _
    for _ in $(seq 60); do
        curl -fsS -o /dev/null --max-time 2 "$1" && return 0
        sleep 1
    done
    return 1
}

# Every socket listening on PORT, by local address.
listeners() { ss -Hltn "sport = :$1" | awk '{print $4}'; }

if [[ ${1:-} == --gone ]]; then
    for unit in "${UNITS[@]}"; do
        [[ ! -e $UNIT_DIR/$unit ]] && ok "$unit removed" || fail "$unit is still in $UNIT_DIR"
    done
    [[ ! -e $DEST ]] && ok "$DEST removed" || fail "$DEST is still there"
    [[ ! -e $HOME/.local/bin/reachy-bridge ]] && ok "the command is removed" || fail "the reachy-bridge command is still there"
    for port in 8000 7861; do
        [[ -z $(listeners "$port") ]] && ok "nothing on port $port" || fail "something still listens on $port"
    done
    exit "$failed"
fi

for unit in reachy-bridge.service reachy-bridge-controller.service; do
    systemctl --user is-active --quiet "$unit" && ok "$unit is running" || fail "$unit isn't running"
    systemctl --user is-enabled --quiet "$unit" && ok "$unit is enabled" || fail "$unit isn't enabled"
done
# The controller starts these on demand.
for unit in reachy-bridge-sim.service reachy-bridge-audio.service; do
    [[ -e $UNIT_DIR/$unit ]] && ok "$unit is installed" || fail "$unit is missing"
    ! systemctl --user is-enabled --quiet "$unit" && ok "$unit isn't enabled" || fail "$unit is enabled"
done
answers http://127.0.0.1:8000/api/daemon/status && ok "the daemon answers" || fail "the daemon doesn't answer on 127.0.0.1:8000"
answers http://127.0.0.1:7861/api/daemon/status && ok "the controller answers" || fail "the controller doesn't answer on 127.0.0.1:7861"
# The daemon has no authentication: loopback only, ever.
for port in 8000 7861; do
    addrs=$(listeners "$port")
    if [[ -n $addrs ]] && ! grep -qvE '^(127\.0\.0\.1|\[::1\]):' <<<"$addrs"; then
        ok "port $port is on loopback only"
    else
        fail "port $port listens on: ${addrs:-nothing}"
    fi
done
power=$("$HOME/.local/bin/reachy-bridge" robot-power) || true
[[ $power == off ]] && ok "the robot starts off" || fail "robot-power says ${power:-nothing}"
[[ $(cat "$DEST/VERSION" 2>/dev/null) == $(git rev-parse HEAD) ]] && ok "VERSION is this checkout" || fail "VERSION isn't HEAD"
[[ -s $DEST/install-manifest ]] && ok "the install manifest is written" || fail "no install manifest"
exit "$failed"
