#!/usr/bin/env bash
# The checks are functions that check() calls by name.
# shellcheck disable=SC2329

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

# check WHAT COMMAND...: prints "ok: WHAT" or "FAIL: WHAT".
check() {
    local what=$1
    shift
    if "$@"; then
        echo "ok: $what"
    else
        echo "FAIL: $what"
        failed=1
    fi
}

# Waits up to a minute for URL to answer 200.
answers() {
    local _
    for _ in $(seq 60); do
        curl -fsS -o /dev/null --max-time 2 "$1" && return 0
        sleep 1
    done
    return 1
}

# Every socket listening on PORT, by local address. Fails if ss does.
listeners() { ss -Hltn "sport = :$1" | awk '{print $4}'; }
nothing_on() {
    local addrs
    addrs=$(listeners "$1") || return 1
    [[ -z $addrs ]]
}
# The daemon has no authentication: loopback only, ever.
loopback_only() {
    local addrs
    addrs=$(listeners "$1") || return 1
    echo "  port $1: ${addrs:-nothing}" | paste -sd' '
    [[ -n $addrs ]] && ! grep -qvE '^(127\.0\.0\.1|\[::1\]):' <<<"$addrs"
}
absent() { [[ ! -e $1 ]]; }
not_enabled() { ! systemctl --user is-enabled --quiet "$1"; }
robot_off() { [[ $("$HOME/.local/bin/reachy-bridge" robot-power) == off ]]; }
version_is_head() { [[ $(cat "$DEST/VERSION" 2>/dev/null) == $(git rev-parse HEAD) ]]; }

if [[ ${1:-} == --gone ]]; then
    for unit in "${UNITS[@]}"; do
        check "$unit is removed" absent "$UNIT_DIR/$unit"
    done
    check "$DEST is removed" absent "$DEST"
    check "the reachy-bridge command is removed" absent "$HOME/.local/bin/reachy-bridge"
    for port in 8000 7861; do
        check "nothing listens on port $port" nothing_on "$port"
    done
    exit "$failed"
fi

for unit in reachy-bridge.service reachy-bridge-controller.service; do
    check "$unit is running" systemctl --user is-active --quiet "$unit"
    check "$unit is enabled" systemctl --user is-enabled --quiet "$unit"
done
# The controller starts these on demand.
for unit in reachy-bridge-sim.service reachy-bridge-audio.service; do
    check "$unit is installed" test -e "$UNIT_DIR/$unit"
    check "$unit isn't enabled" not_enabled "$unit"
done
check "the daemon answers on 127.0.0.1:8000" answers http://127.0.0.1:8000/api/daemon/status
check "the controller answers on 127.0.0.1:7861" answers http://127.0.0.1:7861/api/daemon/status
for port in 8000 7861; do
    check "port $port listens on loopback only" loopback_only "$port"
done
check "the robot starts off" robot_off
check "VERSION is this checkout" version_is_head
check "the install manifest is written" test -s "$DEST/install-manifest"
exit "$failed"
