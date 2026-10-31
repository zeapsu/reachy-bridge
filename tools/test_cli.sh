#!/usr/bin/env bash
# Tests the reachy-bridge command's safety checks: the robot-off gate before a
# daemon restart, sim mode, `deploy <tag>` and its rollback, `serve` and
# `pair`. Runs in a throwaway HOME with curl, systemctl, tailscale and sleep
# replaced by stubs that log their calls, and a scratch git checkout with two
# tags whose install.sh only logs. Changes nothing outside a temp directory.
# Needs bash 4.4+ and git 2.32+. (Uninstall has its own test: test_uninstall.sh.)
#
#   bash tools/test_cli.sh
#
# The checks are single-quoted on purpose: check evals them later.
# shellcheck disable=SC2016
set -euo pipefail

CLI=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy/reachy-bridge
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
fails=0
REPO=
# Leave the developer's git config (signing, hooks, templates) out of it.
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1

fail() { echo "FAIL: $*"; fails=$((fails + 1)); }
check() { if eval "$2"; then echo "ok: $1"; else fail "$1"; fi; }
# refuse NAME TEXT ARGS...: the command fails, saying TEXT.
refuse() {
    local name=$1 text=$2
    shift 2
    if run "$@"; then fail "$name: not refused"
    elif grep -qF -- "$text" "$ROOT/out"; then echo "ok: $name"
    else fail "$name: refused without saying '$text'"; cat "$ROOT/out"; fi
}

stub() {
    printf '#!/usr/bin/env bash\n%s\n' "$2" >"$ROOT/stubs/$1"
    chmod +x "$ROOT/stubs/$1"
}

# What the fake daemon and controller answer, one word per file in $ROOT/state:
#   daemon      down (nothing listening), slow (curl times out), off, on, garbage
#   app         none, running, garbage
#   sim         off, on, down (controller not running), slow, garbage
#   controller  up, down (its status route, for the health check)
set_state() { printf '%s' "$2" >"$ROOT/state/$1"; }

setup() {
    rm -rf "${ROOT:?}/home" "${ROOT:?}/stubs" "${ROOT:?}/state"
    H=$ROOT/home
    mkdir -p "$H/.local/bin" "$H/.local/share/reachy-bridge" "$ROOT/stubs" "$ROOT/state"
    : >"$ROOT/log"
    cp "$CLI" "$H/.local/bin/reachy-bridge"
    set_state daemon off
    set_state app none
    set_state sim off
    set_state controller up
    local log="echo \"\$(basename \"\$0\") \$*\" >>'$ROOT/log'"
    stub systemctl "$log
exit 0"
    stub curl "$log
s='$ROOT/state'
url=\${*: -1}
write=0
for a in \"\$@\"; do [[ \$a == -w ]] && write=1; [[ \$a == @- ]] && cat >>'$ROOT/stdin'; done
reply() { if (( write )); then printf '%s\n%s' \"\$1\" \"\$2\"; else printf '%s' \"\$1\"; fi; }
case \$url in
    *:7861/api/sim)
        case \$(<\$s/sim) in
            down) exit 7 ;;
            slow) exit 28 ;;
            on) reply '{\"active\":true}' 200 ;;
            off) reply '{\"active\":false}' 200 ;;
            *) reply 'oops' 200 ;;
        esac ;;
    *:7861/api/daemon/status)
        [[ \$(<\$s/controller) == up ]] || exit 7
        reply '{\"status\":\"stopped\"}' 200 ;;
    *:7861/api/auth/approve) reply '{\"peer\":\"192.0.2.9\"}' 200 ;;
    *:7861/*) exit 7 ;;
    */api/daemon/status)
        case \$(<\$s/daemon) in
            down) exit 7 ;;
            slow) exit 28 ;;
            off) reply '{\"state\":\"stopped\",\"backend_status\":null}' 200 ;;
            on) reply '{\"state\":\"running\",\"backend_status\":{\"motor_control_mode\":\"enabled\"}}' 200 ;;
            *) reply '<html>' 200 ;;
        esac ;;
    */api/apps/current-app-status)
        [[ \$(<\$s/daemon) == down ]] && exit 7
        case \$(<\$s/app) in
            running) reply '{\"state\":\"running\"}' 200 ;;
            none) reply null 200 ;;
            *) reply 'oops' 502 ;;
        esac ;;
esac"
    stub tailscale "$log
exit 0"
    stub sleep "$log"
    # Never the real thing: a failed tailscale call retries with sudo.
    stub sudo "$log"
    : >"$ROOT/stdin"
}

run() {
    env -u DAEMON_URL -u CONTROLLER_PORT -u CONTROLLER_APP_PORT -u REACHY_BRIDGE_SERVE_PORT \
        HOME="$H" XDG_CONFIG_HOME="" XDG_STATE_HOME="" REACHY_BRIDGE_STATE_DIR="" REACHY_BRIDGE_REPO="${REPO:-$ROOT/none}" \
        PATH="$ROOT/stubs:$PATH" bash "$H/.local/bin/reachy-bridge" "$@" >"$ROOT/out" 2>&1
}

# A clone of a repo with tags v1 and v2, whose install.sh logs the commit it
# installs and exits with the next code in $ROOT/install-rcs (default 0).
make_checkout() {
    local origin=$ROOT/origin
    rm -rf "$origin" "$ROOT/repo"
    git init -q -b main "$origin"
    mkdir -p "$origin/deploy"
    cat >"$origin/deploy/install.sh" <<EOF
#!/usr/bin/env bash
echo "install.sh \$(git -C "\$(dirname "\$0")/.." describe --tags --always)" >>'$ROOT/log'
rc=0
if [[ -s '$ROOT/install-rcs' ]]; then
    rc=\$(head -n 1 '$ROOT/install-rcs')
    tail -n +2 '$ROOT/install-rcs' >'$ROOT/install-rcs.tmp'
    mv '$ROOT/install-rcs.tmp' '$ROOT/install-rcs'
fi
exit "\$rc"
EOF
    chmod +x "$origin/deploy/install.sh"
    for tag in v1 v2; do
        echo "$tag" >"$origin/VERSION.txt"
        git -C "$origin" add -A
        git -C "$origin" -c user.name=t -c user.email=t@example.com commit -q -m "$tag"
        git -C "$origin" tag "$tag"
    done
    git clone -q "$origin" "$ROOT/repo"
    git -C "$ROOT/repo" checkout -q v1
    git -C "$ROOT/repo" switch -q -c main-local
    : >"$ROOT/install-rcs"
    REPO=$ROOT/repo
}

echo "== robot-power fails closed"
setup
for case in "down off" "off off" "on on" "slow unknown" "garbage unknown"; do
    read -r daemon want <<<"$case"
    set_state daemon "$daemon"
    run robot-power || true
    check "daemon $daemon -> $want" '[[ $(<"$ROOT/out") == "$want" ]]'
done

echo "== restart: the robot must be off"
setup
set_state daemon on
if run restart; then fail "restarted with the robot on"; else echo "ok: refused"; fi
check "says why" 'grep -q "isn.t confirmed off" "$ROOT/out"'
check "restarts nothing" '! grep -q "systemctl --user restart" "$ROOT/log"'
set_state daemon slow
if run restart daemon; then fail "restarted with the robot unknown"; else echo "ok: refused when unknown"; fi
check "a slow daemon restarts nothing" '! grep -q "systemctl --user restart" "$ROOT/log"'
set_state daemon on
run restart controller || fail "controller restart exited $?"
check "the controller alone restarts with the robot on" 'grep -q "systemctl --user restart reachy-bridge-controller.service" "$ROOT/log"'
: >"$ROOT/log"
run restart --force || fail "forced restart exited $?"
check "--force restarts both" 'grep -q "systemctl --user restart reachy-bridge.service reachy-bridge-controller.service" "$ROOT/log"'
set_state daemon off
: >"$ROOT/log"
run restart || fail "restart with the robot off exited $?"
check "restarts with the robot off" 'grep -q "systemctl --user restart reachy-bridge.service" "$ROOT/log"'

echo "== sim mode belongs to the dashboard"
setup
refuse "refuses the sim unit" "the sim is started from the dashboard" start sim
set_state sim on
refuse "refuses the daemon in sim mode" "sim mode is on;" start daemon
set_state sim garbage
refuse "refuses an unreadable sim answer" "can't tell whether sim mode is on" start
set_state sim slow
refuse "refuses a controller too slow to say" "can't tell whether sim mode is on" start
check "starts nothing" '! grep -q "systemctl --user start" "$ROOT/log"'
set_state sim down
run start || fail "start with no controller exited $?"
check "no controller means no sim mode" 'grep -q "systemctl --user start reachy-bridge.service reachy-bridge-controller.service" "$ROOT/log"'

echo "== serve uses only the dedicated ports"
setup
run serve on || fail "serve on exited $?"
check "maps the dashboard and app ports" 'grep -q "tailscale serve --bg --https=10000 http://127.0.0.1:7861" "$ROOT/log" && grep -q "tailscale serve --bg --https=8043 http://127.0.0.1:7863" "$ROOT/log"'
run serve off || fail "serve off exited $?"
check "unmaps the same two ports" 'grep -q "tailscale serve --https=10000 off" "$ROOT/log" && grep -q "tailscale serve --https=8043 off" "$ROOT/log"'
check "never the root mapping, 8443 or a reset" '! grep -qE "https=(443|8443) |serve reset" "$ROOT/log"'

echo "== pair"
setup
if run pair 12345; then fail "took a 5-digit code"; else echo "ok: refused a short code"; fi
run pair 123456 'Sam "phone"' || fail "pair exited $?"
check "sends the code and an escaped name" 'grep -qF "\"code\": \"123456\", \"name\": \"Sam \\\"phone\\\"\"" "$ROOT/log"'
if run pair --admin 123456; then fail "approved an admin without the local key"; else echo "ok: --admin needs the local key"; fi

echo "== deploy refuses an unsafe start"
setup
make_checkout
set_state daemon on
refuse "refuses with the robot on" "isn't confirmed off" deploy v2
set_state daemon off
set_state app running
refuse "refuses with an app running" "an app is running" deploy v2
set_state app garbage
refuse "refuses when the daemon can't say" "an app is running, or the daemon didn't say" deploy v2
set_state app none
# A file both tags share, so only the guard can stop it.
echo dirty >>"$REPO/deploy/install.sh"
refuse "refuses local changes" "has local changes" deploy v2
git -C "$REPO" checkout -q -- deploy/install.sh
refuse "refuses a missing tag" "no tag v9" deploy v9
refuse "refuses --force" "usage: reachy-bridge deploy" deploy v2 --force
check "ran no install" '! grep -q "install.sh" "$ROOT/log"'

echo "== deploy installs a tag"
setup
make_checkout
run deploy v2 || { fail "deploy exited $?"; cat "$ROOT/out"; }
check "installs v2 once" '[[ $(grep -c "install.sh" "$ROOT/log") == 1 ]] && grep -q "install.sh v2" "$ROOT/log"'
check "records what it installed" '[[ $(<"$H/.local/share/reachy-bridge/VERSION") == $(git -C "$REPO" rev-parse v2) ]]'

echo "== deploy rolls back a failed install"
setup
make_checkout
git -C "$REPO" rev-parse v1 >"$H/.local/share/reachy-bridge/VERSION"
echo 1 >"$ROOT/install-rcs"
if run deploy v2; then fail "reported success"; else echo "ok: reported the failure"; fi
check "reinstalls v1 after v2" 'grep -q "install.sh v2" "$ROOT/log" && [[ $(grep "install.sh" "$ROOT/log" | tail -n 1) == "install.sh v1" ]]'
check "says v1 is back" 'grep -q "v1 is installed again" "$ROOT/out"'
check "keeps the v1 record" '[[ $(<"$H/.local/share/reachy-bridge/VERSION") == $(git -C "$REPO" rev-parse v1) ]]'

echo "== deploy rolls back an unhealthy install"
setup
make_checkout
git -C "$REPO" rev-parse v1 >"$H/.local/share/reachy-bridge/VERSION"
set_state controller down
if run deploy v2; then fail "reported success"; else echo "ok: reported the failure"; fi
check "reinstalls the previous version" '[[ $(grep -c "install.sh" "$ROOT/log") == 2 && $(grep "install.sh" "$ROOT/log" | tail -n 1) == "install.sh v1" ]]'
check "forgets the record when nothing is healthy" '[[ ! -e $H/.local/share/reachy-bridge/VERSION ]]'

echo "== deploy refused by install.sh changes nothing"
setup
make_checkout
echo 3 >"$ROOT/install-rcs"
if run deploy v2; then fail "reported success"; else echo "ok: reported the refusal"; fi
check "no rollback install" '[[ $(grep -c "install.sh" "$ROOT/log") == 1 ]]'
check "the checkout is back on its branch" '[[ $(git -C "$REPO" symbolic-ref --short HEAD) == main-local ]]'

echo "== deploy rolls back to what was installed, not the checkout"
setup
make_checkout
git -C "$REPO" rev-parse v2 >"$H/.local/share/reachy-bridge/VERSION"
git -C "$REPO" tag v3 v1
echo 1 >"$ROOT/install-rcs"
if run deploy v3; then fail "reported success"; else echo "ok: reported the failure"; fi
check "reinstalls v2, the recorded version" '[[ $(grep "install.sh" "$ROOT/log" | tail -n 1) == "install.sh v2" ]]'
check "says the checkout is detached" 'grep -q "detached at v2" "$ROOT/out"'

echo "== the local key goes over stdin"
setup
mkdir -p "$H/.local/state/reachy-bridge"
echo secret-key-123 >"$H/.local/state/reachy-bridge/controller.key"
run start || fail "start exited $?"
run pair 123456 || fail "pair exited $?"
check "sends the key header" '(( $(grep -c "X-Reachy-Bridge-Key: secret-key-123" "$ROOT/stdin") == 2 ))'
check "never in the arguments" '! grep -q secret-key-123 "$ROOT/log"'

echo "== unknown commands"
setup
refuse "refuses an unknown command" "unknown command 'frobnicate'" frobnicate
refuse "refuses an unknown service" "unknown service 'robot'" start robot

if (( fails )); then echo "$fails failed"; exit 1; fi
echo "all passed"
