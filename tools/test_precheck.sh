#!/usr/bin/env bash
# Tests for tools/precheck.sh's hook mode: which tool calls it checks, and
# that it stays out of the way when off.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
fail=0
check() {
    local want=$1 cmd=$2 got
    got=$(jq -n --arg c "$cmd" '{session_id: "t", tool_input: {command: $c}}' |
        PRECHECK_MATCH_ONLY=1 REACHY_PRECHECK_MODE=observe bash tools/precheck.sh --hook)
    if [[ $got == "$want" ]]; then echo "ok   $want: $cmd"; else echo "FAIL want $want, got '$got': $cmd"; fail=1; fi
}
check match 'git push -u origin feat/1'
check match 'git push'
check match 'cd x && git push origin HEAD'
check match 'git -C /tmp/r push'
check match 'gh pr create --draft --title t'
check match '(git push)'
check skip 'git status'
check skip 'git commit -m "push later"'
check skip 'echo git pushed'
check skip 'gh pr view 3'
check skip 'git push-something'
check skip ''

got=$(jq -n '{tool_input: {command: "git push"}}' | REACHY_PRECHECK_MODE=off PRECHECK_MATCH_ONLY=1 bash tools/precheck.sh --hook)
if [[ -z $got ]]; then echo "ok   off does nothing"; else echo "FAIL off printed '$got'"; fail=1; fi

if bash tools/precheck.sh --bogus 2>/dev/null; then echo "FAIL unknown flag accepted"; fail=1; else echo "ok   unknown flag refused"; fi
exit $fail
