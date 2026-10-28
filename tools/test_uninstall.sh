#!/usr/bin/env bash
# Tests `reachy-bridge uninstall` (#142) in a throwaway HOME, with systemctl,
# curl, tailscale and sudo replaced by stubs that log their calls. Changes
# nothing outside a temp directory. Needs bash 4.4+ and python3.
#
#   bash tools/test_uninstall.sh
#
# The checks are single-quoted on purpose: check evals them later.
# shellcheck disable=SC2016
set -euo pipefail

CLI=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy/reachy-bridge
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
fails=0

fail() { echo "FAIL: $*"; fails=$((fails + 1)); }
check() { if eval "$2"; then echo "ok: $1"; else fail "$1"; fi; }

stub() {
    printf '#!/usr/bin/env bash\n%s\n' "$2" >"$ROOT/stubs/$1"
    chmod +x "$ROOT/stubs/$1"
}

# A fresh fake install. $1: "manifest" or "old" (installed before manifests).
setup() {
    rm -rf "${ROOT:?}/home" "${ROOT:?}/stubs" "${ROOT:?}/etc"
    H=$ROOT/home
    D=$H/.local/share/reachy-bridge
    U=$H/.config/systemd/user
    S=$H/.local/state/reachy-bridge
    mkdir -p "$D/.venv" "$D/bin" "$D/sim" "$D/apps_venv/lib" "$U" "$S/sim" "$H/.local/bin" \
        "$H/.local/share/gstreamer-1.0/plugins" "$ROOT/stubs" "$ROOT/etc/udev"
    : >"$ROOT/log"
    echo keep >"$H/unrelated"
    echo abc >"$D/VERSION"
    echo key >"$S/controller.key"
    cp "$CLI" "$H/.local/bin/reachy-bridge"
    for u in reachy-bridge.service reachy-bridge-controller.service reachy-bridge-sim.service reachy-bridge-update.timer; do
        echo "[Unit]" >"$U/$u"
    done
    echo new >"$H/.local/share/gstreamer-1.0/plugins/libgstrswebrtc.so"
    echo old >"$H/.local/share/gstreamer-1.0/plugins/libgstrswebrtc.so.bak"
    echo rule >"$ROOT/etc/udev/99-reachy.rules"
    if [[ $1 == manifest ]]; then
        printf '%s\t%s\n' \
            dir "$D" file "$H/.local/bin/reachy-bridge" \
            unit reachy-bridge.service unit reachy-bridge-controller.service \
            unit reachy-bridge-sim.service unit reachy-bridge-update.timer \
            udev "$ROOT/etc/udev/99-reachy.rules" group dialout linger 1 \
            gst-backup "$H/.local/share/gstreamer-1.0/plugins/libgstrswebrtc.so $H/.local/share/gstreamer-1.0/plugins/libgstrswebrtc.so.bak" \
            dir /etc/reachy-bridge >"$D/install-manifest"
    fi
    local log="echo \"\$(basename \"\$0\") \$*\" >>'$ROOT/log'"
    # The daemon is up with an app running and the robot on, until told to stop.
    stub systemctl "$log
[[ \$1 == --user && \$2 == is-active ]] && exit \$([[ -e '$ROOT/stopped' ]] && echo 3 || echo 0)
exit 0"
    stub curl "$log
url=\${*: -1}
case \$url in
    */api/apps/stop-current-app) touch '$ROOT/app-stopped' ;;
    */api/daemon/stop*) touch '$ROOT/robot-off' ;;
    */api/apps/current-app-status)
        if [[ -e '$ROOT/app-stopped' ]]; then printf 'null\n200'; else printf '{\"state\":\"running\"}\n200'; fi ;;
    */api/daemon/status)
        if [[ -e '$ROOT/robot-off' ]]; then echo '{\"state\":\"stopped\",\"backend_status\":null}'
        else echo '{\"state\":\"running\",\"backend_status\":{}}'; fi ;;
esac"
    # Port 10000 serves the bridge; 8043 was reused by something else.
    stub tailscale "$log
[[ \$1 == serve && \$2 == status ]] && echo '{\"Web\":{\"n.ts.net:10000\":{\"Handlers\":{\"/\":{\"Proxy\":\"http://127.0.0.1:7861\"}}},\"n.ts.net:8043\":{\"Handlers\":{\"/\":{\"Proxy\":\"http://127.0.0.1:9999\"}}},\"n.ts.net:443\":{\"Handlers\":{\"/\":{\"Proxy\":\"http://127.0.0.1:3000\"}}}}}'
exit 0"
    stub sudo "$log
\"\$@\""
    for tool in udevadm gpasswd loginctl sleep; do stub "$tool" "$log"; done
    rm -f "$ROOT/app-stopped" "$ROOT/robot-off" "$ROOT/stopped"
}

run() {
    HOME=$H XDG_CONFIG_HOME='' XDG_STATE_HOME='' REACHY_BRIDGE_STATE_DIR='' PATH="$ROOT/stubs:$PATH" USER=tester \
        bash "$H/.local/bin/reachy-bridge" uninstall "$@" >"$ROOT/out" 2>&1
}

echo "== dry run"
setup manifest
run --dry-run || fail "dry run exited $?"
check "dry run removes nothing" '[[ -d $D/.venv && -f $U/reachy-bridge.service && -d $S ]]'
check "dry run touches no service or robot" '! grep -qE "^(systemctl --user (stop|disable)|curl .*-X POST|sudo)" "$ROOT/log"'
check "dry run lists the bridge port only" 'grep -q "tailnet HTTPS ports: 10000 " "$ROOT/out"'

echo "== refuses without a terminal or --yes"
setup manifest
if run </dev/null; then fail "ran without --yes"; else echo "ok: refused"; fi
check "refusal removes nothing" '[[ -d $D/.venv ]]'

echo "== full uninstall"
setup manifest
run --yes || { fail "uninstall exited $?"; cat "$ROOT/out"; }
check "stops the app first" 'grep -q "stop-current-app" "$ROOT/log"'
check "puts the robot to sleep" 'grep -q "daemon/stop?goto_sleep=true" "$ROOT/log"'
check "robot sleeps before the services stop" '(( $(grep -n "daemon/stop" "$ROOT/log" | cut -d: -f1) < $(grep -n "disable --now reachy-bridge.service" "$ROOT/log" | cut -d: -f1) ))'
check "disables every unit, the timer too" '(( $(grep -c "systemctl --user disable --now" "$ROOT/log") == 4 ))'
check "removes the units" '[[ -z $(ls "$U") ]]'
check "removes only the bridge serve port" 'grep -q "tailscale serve --https=10000 off" "$ROOT/log" && ! grep -qE "https=(8043|443) off" "$ROOT/log"'
check "never resets tailscale serve" '! grep -q "serve reset" "$ROOT/log"'
check "removes the udev rule" '[[ ! -e $ROOT/etc/udev/99-reachy.rules ]] && grep -q "udevadm control --reload-rules" "$ROOT/log"'
check "undoes dialout and linger" 'grep -q "gpasswd -d tester dialout" "$ROOT/log" && grep -q "loginctl disable-linger tester" "$ROOT/log"'
check "restores the GStreamer plugin" '[[ $(<"$H/.local/share/gstreamer-1.0/plugins/libgstrswebrtc.so") == old ]]'
check "removes the install, apps and state" '[[ ! -e $D && ! -e $S ]]'
check "removes the CLI" '[[ ! -e $H/.local/bin/reachy-bridge ]]'
check "skips a manifest path outside HOME" 'grep -q "skipped /etc/reachy-bridge" "$ROOT/out"'
check "leaves unrelated files" '[[ -f $H/unrelated ]]'

echo "== --keep-data"
setup manifest
run --yes --keep-data || { fail "keep-data exited $?"; cat "$ROOT/out"; }
check "keeps apps_venv and state" '[[ -d $D/apps_venv/lib && -f $S/controller.key ]]'
check "removes the rest of the install" '[[ ! -e $D/.venv && ! -e $D/bin && ! -e $D/VERSION && ! -e $D/install-manifest ]]'

echo "== installed before the manifest"
setup old
touch "$ROOT/stopped"  # the daemon isn't running
run --yes || { fail "old uninstall exited $?"; cat "$ROOT/out"; }
check "removes the three units" '[[ ! -e $U/reachy-bridge.service && ! -e $U/reachy-bridge-controller.service && ! -e $U/reachy-bridge-sim.service ]]'
check "leaves units it didn't record" '[[ -f $U/reachy-bridge-update.timer ]]'
check "no robot calls with the daemon down" '! grep -q "curl .*-X POST" "$ROOT/log"'
check "no sudo for system changes it didn't record" '! grep -qE "sudo (gpasswd|loginctl|udevadm|rm)" "$ROOT/log"'
check "removes the install and CLI" '[[ ! -e $D && ! -e $H/.local/bin/reachy-bridge ]]'

if (( fails )); then echo "$fails failed"; exit 1; fi
echo "all passed"
