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

# A full run under /bin/bash (3.2 on macOS, #336) in a scratch repo whose
# branch changes one tools/ script: the shellcheck step gets the repo's
# scripts. The stub shellcheck logs its arguments; no mise on PATH, so the
# stub is what runs.
here=$PWD
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch/bin" "$scratch/repo/tools"
printf '#!/bin/sh\necho "$@" >>"%s/shellcheck.log"\n' "$scratch" >"$scratch/bin/shellcheck"
chmod +x "$scratch/bin/shellcheck"
(
    cd "$scratch/repo"
    export GIT_CONFIG_NOSYSTEM=1 HOME=$scratch GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.com \
        GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.com
    git init -q
    echo 'echo a' >tools/a.sh; echo 'echo b' >tools/b.sh
    git add tools; git commit -qm base
    echo 'echo a2' >tools/a.sh; git commit -qam change
)
out=$(cd "$scratch/repo" && PATH="$scratch/bin:/usr/bin:/bin" PRECHECK_BASE=HEAD~1 \
    /bin/bash "$here/tools/precheck.sh" 2>&1) && rc=0 || rc=$?
if [[ $rc == 0 && $(cat "$scratch/shellcheck.log" 2>/dev/null) == "tools/a.sh tools/b.sh" ]]; then
    echo "ok   full run under /bin/bash $(/bin/bash -c 'echo $BASH_VERSION')"
else
    echo "FAIL full run under /bin/bash: exit $rc, shellcheck got '$(cat "$scratch/shellcheck.log" 2>/dev/null)'"
    echo "$out"; fail=1
fi
exit $fail
