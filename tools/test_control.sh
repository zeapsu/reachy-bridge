#!/usr/bin/env bash
# Tests `reachy-bridge control` in a throwaway HOME, with curl replaced by a
# stub that answers like the controller and logs each request. Changes
# nothing outside a temp directory. Needs bash 4.4+ and python3.
#
#   bash tools/test_control.sh
#
# The checks are single-quoted on purpose: check evals them later.
# shellcheck disable=SC2016
set -euo pipefail

CLI=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy/reachy-bridge
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
fails=0
H=$ROOT/home
S=$H/.local/state/reachy-bridge

fail() { echo "FAIL: $*"; fails=$((fails + 1)); }
check() { if eval "$2"; then echo "ok: $1"; else fail "$1"; fi; }

mkdir -p "$S" "$ROOT/stubs"
echo testkey >"$S/controller.key"
# Logs "METHOD URL BODY" and the key header it was given on stdin; answers
# from the files in $ROOT, with the status in $ROOT/status (default 200).
cat >"$ROOT/stubs/curl" <<EOF
#!/usr/bin/env bash
method=GET data= url=\${*: -1}
while (( \$# )); do
    case \$1 in
        -X) method=\$2; shift ;;
        --data) data=\$2; shift ;;
    esac
    shift
done
key=\$(cat)
echo "\$method \$url \$data | \$key" >>'$ROOT/log'
status=\$(cat '$ROOT/status' 2>/dev/null || echo 200)
case \$url in
    */api/auth/devices) cat '$ROOT/devices' ;;
    */api/control) cat '$ROOT/control' ;;
    */api/control/grants) [[ \$status == 2* ]] && echo '{"id":"aaaaaaaaaaaa"}' || echo '{"detail":"that overlaps Desk'"'"'s control time"}' ;;
esac
printf '\n%s' "\$status"
EOF
chmod +x "$ROOT/stubs/curl"

cat >"$ROOT/devices" <<'EOF'
{"devices":[{"id":"111111111111","name":"Phone","person":"Andry","role":"admin","last_seen":0,"peer":"x"},{"id":"222222222222","name":"Laptop","person":"Guest \"G\"","last_seen":0,"peer":"y"}]}
EOF
cat >"$ROOT/control" <<'EOF'
{"admin_mode":true,"now":0,"holder":{"person":"Desk","device":"Mac","admin":false,"since":0,"until":null},"grants":[{"id":"0123456789ab","person":"Desk","from":0,"until":null,"by":"x"}],"requests":[{"person":"Guest","device":"222222222222","at":0,"minutes":30,"note":"hi"}]}
EOF

run() {
    : >"$ROOT/log"
    HOME=$H XDG_STATE_HOME='' REACHY_BRIDGE_STATE_DIR='' PATH="$ROOT/stubs:$PATH" \
        bash "$CLI" control "$@" >"$ROOT/out" 2>&1
}

echo "== list"
run || { fail "list exited $?"; cat "$ROOT/out"; }
check "shows the holder" 'grep -q "in control: Desk on Mac" "$ROOT/out"'
check "numbers the grants" 'grep -q "1  0123456789ab  Desk, .* to no end" "$ROOT/out"'
check "shows requests" 'grep -q "Guest on Laptop, 30 min: hi" "$ROOT/out"'
check "sends the local key" 'grep -q "| X-Reachy-Bridge-Key: testkey" "$ROOT/log"'
cp "$ROOT/control" "$ROOT/control.bak"
echo '{"admin_mode":false}' >"$ROOT/control"
run || fail "list without admin exited $?"
check "says when there's no admin" 'grep -q "no admin yet" "$ROOT/out"'
mv "$ROOT/control.bak" "$ROOT/control"

echo "== grant by person, no end"
run grant Andry || { fail "grant exited $?"; cat "$ROOT/out"; }
check "posts the person with no minutes" 'grep -qx "POST http://127.0.0.1:7861/api/control/grants {\"person\": \"Andry\"} | X-Reachy-Bridge-Key: testkey" "$ROOT/log"'
check "says no end" 'grep -q "granted Andry control time (no end)" "$ROOT/out"'

echo "== grant by device number, with minutes"
run grant 2 045 || { fail "grant exited $?"; cat "$ROOT/out"; }
check "grants the device's person, quoted" 'grep -q "POST .*/api/control/grants {\"person\": \"Guest \\\\\"G\\\\\"\", \"minutes\": 45}" "$ROOT/log"'
check "says the minutes" 'grep -q "(45 min)" "$ROOT/out"'

echo "== refusals"
if run grant 9; then fail "granted a missing device"; else check "no such device" 'grep -q "no device 9" "$ROOT/out"'; fi
if run grant Andry soon; then fail "took bad minutes"; else check "bad minutes" 'grep -q "minutes is a number" "$ROOT/out" && ! grep -q POST "$ROOT/log"'; fi
if run grant Andry ""; then fail "took empty minutes as no end"; else check "empty minutes" 'grep -q "minutes is a number" "$ROOT/out" && ! grep -q POST "$ROOT/log"'; fi
if run grant; then fail "granted nobody"; else check "usage without a person" 'grep -q "usage: reachy-bridge control grant" "$ROOT/out"'; fi
echo 409 >"$ROOT/status"
if run grant Andry; then fail "ignored a 409"; else check "shows the controller's reason" 'grep -q "overlaps Desk" "$ROOT/out"'; fi
rm "$ROOT/status"
mv "$S/controller.key" "$S/key.bak"
if run grant Andry; then fail "granted without the key"; else check "needs the local key" 'grep -q "can.t read .*controller.key" "$ROOT/out" && [[ ! -s $ROOT/log ]]'; fi
mv "$S/key.bak" "$S/controller.key"

echo "== revoke"
run revoke 1 || { fail "revoke exited $?"; cat "$ROOT/out"; }
check "revokes the numbered grant" 'grep -q "^DELETE http://127.0.0.1:7861/api/control/grants/0123456789ab " "$ROOT/log"'
check "names whose grant" 'grep -q "revoked grant 0123456789ab (Desk)" "$ROOT/out"'
run revoke 0123456789ab || fail "revoke by id exited $?"
check "revokes by id without a lookup" 'grep -q "^DELETE .*/api/control/grants/0123456789ab " "$ROOT/log" && ! grep -q "^GET" "$ROOT/log"'
if run revoke ../x; then fail "took a bad id"; else check "rejects a bad id" '! grep -q DELETE "$ROOT/log"'; fi
run revoke Desk || fail "revoke by person exited $?"
check "revokes the person's grant" 'grep -q "^DELETE .*/api/control/grants/0123456789ab " "$ROOT/log" && grep -q "(Desk)" "$ROOT/out"'
if run revoke 2; then fail "revoked a missing grant"; else check "says grant numbers aren't devices" 'grep -q "not devices" "$ROOT/out" && ! grep -q DELETE "$ROOT/log"'; fi
if run revoke Nobody; then fail "revoked for nobody"; else check "no grant for a person" 'grep -q "no grant for Nobody" "$ROOT/out"'; fi
cp "$ROOT/control" "$ROOT/control.bak"
sed -i 's/"grants":\[/"grants":[{"id":"ba9876543210","person":"Desk","from":9,"until":null,"by":"x"},/' "$ROOT/control"
if run revoke Desk; then fail "revoked one of several grants by name"; else check "asks for a number when a person has several grants" 'grep -q "more than one grant" "$ROOT/out" && ! grep -q DELETE "$ROOT/log"'; fi
mv "$ROOT/control.bak" "$ROOT/control"

if (( fails )); then echo "$fails failed"; exit 1; fi
echo "all passed"
