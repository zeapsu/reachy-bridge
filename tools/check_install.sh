#!/usr/bin/env bash
# The checks are functions that check() calls by name.
# shellcheck disable=SC2329

# What deploy/install.sh should leave behind, checked on the machine it ran
# on (CI's install job, #201). With --gone: what the uninstaller should.
#
#   bash tools/check_install.sh [--gone]
set -euo pipefail

DEST=$HOME/.local/share/reachy-bridge
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/reachy-bridge
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
UNITS=(reachy-bridge.service reachy-bridge-controller.service reachy-bridge-sim.service
    reachy-bridge-audio.service reachy-bridge-mic.service)
# Daemon, controller, sim daemon, audio gateway.
PORTS=(8000 7861 8001 8002)
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
# No process of any installed unit listens off loopback on any TCP port
# (app UIs, WebRTC signalling and the like included).
units_on_loopback() {
    local unit cgroup pids=() sockets addr pid bad=0
    for unit in "${UNITS[@]}"; do
        cgroup=$(systemctl --user show "$unit" -p ControlGroup --value)
        [[ -n $cgroup && -r /sys/fs/cgroup$cgroup/cgroup.procs ]] || continue
        mapfile -t -O "${#pids[@]}" pids < <(find "/sys/fs/cgroup$cgroup" -name cgroup.procs -exec cat {} +)
    done
    ((${#pids[@]})) || { echo "  no unit processes found"; return 1; }
    sockets=$(ss -Hltnp) || return 1
    while read -r addr pid; do
        [[ " ${pids[*]} " == *" $pid "* ]] || continue
        [[ $addr =~ ^(127\.0\.0\.1|\[::1\]): ]] && continue
        echo "  pid $pid listens on $addr"
        bad=1
    done < <(awk '{ a = $4; while (match($0, /pid=[0-9]+/)) { print a, substr($0, RSTART + 4, RLENGTH - 4); $0 = substr($0, RSTART + RLENGTH) } }' <<<"$sockets")
    ((!bad))
}
absent() { [[ ! -e $1 && ! -L $1 ]]; }
is_static() { [[ $(systemctl --user is-enabled "$1" 2>/dev/null) == static ]]; }
# Running, and not restarting over 5 s.
steady() {
    local before
    systemctl --user is-active --quiet "$1" || return 1
    before=$(systemctl --user show "$1" -p NRestarts --value)
    sleep 5
    systemctl --user is-active --quiet "$1" &&
        [[ $(systemctl --user show "$1" -p NRestarts --value) == "$before" ]]
}
robot_off() { [[ $("$HOME/.local/bin/reachy-bridge" robot-power) == off ]]; }
version_is_head() {
    local head
    head=$(git rev-parse HEAD) && [[ -s $DEST/VERSION && $(<"$DEST/VERSION") == "$head" ]]
}
# Each thing install.sh records, once, even after a second run.
manifest_complete() {
    local manifest=$DEST/install-manifest unit
    [[ -s $manifest ]] || return 1
    grep -qxF "dir"$'\t'"$DEST" "$manifest" || return 1
    grep -qxF "file"$'\t'"$HOME/.local/bin/reachy-bridge" "$manifest" || return 1
    for unit in "${UNITS[@]}"; do
        grep -qxF "unit"$'\t'"$unit" "$manifest" || return 1
    done
    [[ -z $(sort "$manifest" | uniq -d) ]]
}
no_wants_links() { [[ -z $(find "$UNIT_DIR" -name 'reachy-bridge*' 2>/dev/null) ]]; }
none_loaded() {
    local units
    units=$(systemctl --user list-units --all --plain --no-legend 'reachy-bridge*') || return 1
    [[ -z $units ]]
}

if [[ ${1:-} == --gone ]]; then
    for unit in "${UNITS[@]}"; do
        check "$unit is removed" absent "$UNIT_DIR/$unit"
    done
    check "no reachy-bridge files or links are left in $UNIT_DIR" no_wants_links
    check "no reachy-bridge unit is loaded" none_loaded
    check "$DEST is removed" absent "$DEST"
    check "$STATE_DIR is removed" absent "$STATE_DIR"
    check "the reachy-bridge command is removed" absent "$HOME/.local/bin/reachy-bridge"
    for port in "${PORTS[@]}"; do
        check "nothing listens on port $port" nothing_on "$port"
    done
    exit "$failed"
fi

# First, so a unit still starting up gets its minute.
check "the daemon answers on 127.0.0.1:8000" answers http://127.0.0.1:8000/api/daemon/status
check "the controller answers on 127.0.0.1:7861" answers http://127.0.0.1:7861/api/daemon/status
for unit in reachy-bridge.service reachy-bridge-controller.service reachy-bridge-mic.service; do
    check "$unit is running steadily" steady "$unit"
    check "$unit is enabled" systemctl --user is-enabled --quiet "$unit"
done
# The controller starts these on demand; they have no [Install] section.
for unit in reachy-bridge-sim.service reachy-bridge-audio.service; do
    check "$unit is installed" test -e "$UNIT_DIR/$unit"
    check "$unit is static (started on demand only)" is_static "$unit"
done
for port in 8000 7861; do
    check "port $port listens on loopback only" loopback_only "$port"
done
check "every unit's listeners are on loopback" units_on_loopback
check "the robot starts off" robot_off
check "VERSION is this checkout" version_is_head
check "the install manifest records everything once" manifest_complete
exit "$failed"
