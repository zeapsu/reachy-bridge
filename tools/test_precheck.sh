#!/usr/bin/env bash
# Tests for tools/precheck.sh: which tool calls hook mode checks, that it stays
# out of the way when off, and full runs in a scratch repo.
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

# CI's bash is 5, so it can't catch a bash-4-only builtin coming back (#336, #338).
if grep -nE '(^|[^[:alnum:]_])(mapfile|readarray)([^[:alnum:]_]|$)' tools/precheck.sh | grep -v '^[0-9]*:[[:space:]]*#'; then
    echo "FAIL precheck.sh uses mapfile/readarray, which macOS's bash 3.2 lacks"; fail=1
else
    echo "ok   no mapfile/readarray in precheck.sh"
fi

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
# git in the scratch repo, away from the developer's config.
rgit() {
    (cd "$scratch/repo" && GIT_CONFIG_NOSYSTEM=1 HOME=$scratch GIT_AUTHOR_NAME=t \
        GIT_AUTHOR_EMAIL=t@example.com GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.com git "$@")
}
rgit init -q
echo 'echo a' >"$scratch/repo/tools/a.sh"; echo 'echo b' >"$scratch/repo/tools/b.sh"
rgit add tools; rgit commit -qm base
echo 'echo a2' >"$scratch/repo/tools/a.sh"; rgit commit -qam change
out=$(cd "$scratch/repo" && PATH="$scratch/bin:/usr/bin:/bin" PRECHECK_BASE=HEAD~1 \
    /bin/bash "$here/tools/precheck.sh" 2>&1) && rc=0 || rc=$?
if [[ $rc == 0 && $(cat "$scratch/shellcheck.log" 2>/dev/null) == "tools/a.sh tools/b.sh" ]]; then
    echo "ok   full run under /bin/bash $(/bin/bash -c 'echo $BASH_VERSION')"
else
    echo "FAIL full run under /bin/bash: exit $rc, shellcheck got '$(cat "$scratch/shellcheck.log" 2>/dev/null)'"
    echo "$out"; fail=1
fi

# The deploy tests need bash 4.4+: with only an old bash about, precheck says so
# and fails the step instead of running them under it. The stub bash fails the
# version check, as bash 3.2 does.
mkdir -p "$scratch/old" "$scratch/repo/deploy"
printf '#!/bin/sh\nexit 1\n' >"$scratch/old/bash"
chmod +x "$scratch/old/bash"
echo 'echo d' >"$scratch/repo/deploy/x.sh"; rgit add deploy; rgit commit -qm deploy
out=$(cd "$scratch/repo" && PATH="$scratch/bin:/usr/bin:/bin" PRECHECK_BASE=HEAD~1 \
    PRECHECK_BASHES="$scratch/old/bash" /bin/bash "$here/tools/precheck.sh" 2>&1) && rc=0 || rc=$?
if [[ $rc != 0 && $out == *"tools/test_cli.sh needs bash 4.4+"* && $out == *"precheck failed: test_cli test_uninstall" ]]; then
    echo "ok   deploy tests refuse an old bash"
else
    echo "FAIL deploy tests with an old bash: exit $rc"; echo "$out"; fail=1
fi
exit $fail
