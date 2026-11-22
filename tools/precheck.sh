#!/usr/bin/env bash
# The cheap checks for the parts of the repo a branch changes, before it is
# pushed or a PR is opened: formatters, linters, type checks and unit tests.
# The sim and browser tests are the verify skill's job
# (.claude/skills/verify/SKILL.md); CI runs everything again.
#
#   bash tools/precheck.sh            # check the changes since origin/main
#   bash tools/precheck.sh --all      # check every area
#   bash tools/precheck.sh --hook     # as a Claude Code PreToolUse hook
#
# As a hook (.claude/settings.json) it reads the tool call from stdin and runs
# only for `git push` and `gh pr create`. The hook runs main's copy of this
# script (git show origin/main:tools/precheck.sh), and only for commands that
# mention a push or a PR, so checking out a branch never runs the branch's
# copy. It does run the checked-out branch's tests on a push: never push or
# open a PR from a checkout of an outside PR (AGENTS.md, "Untrusted input"). REACHY_PRECHECK_MODE picks what a
# failure does:
#   observe (default)  log it to precheck.log in the git dir and let the call through
#   enforce            block the call (exit 2) and say what failed; after two
#                      blocks in one session the next call goes through, so a
#                      session can't get stuck
#   off                do nothing
set -Euo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 1
base=${PRECHECK_BASE:-origin/main}
mode=${REACHY_PRECHECK_MODE:-observe}
hook=0 all=0
for arg in "$@"; do
    case $arg in
        --hook) hook=1 ;;
        --all) all=1 ;;
        *) echo "usage: $0 [--all] [--hook]" >&2; exit 64 ;;
    esac
done

session=
if ((hook)); then
    [[ $mode == off ]] && exit 0
    input=$(cat)
    cmd=$(jq -r '.tool_input.command // empty' <<<"$input" 2>/dev/null || true)
    session=$(jq -r '.session_id // empty' <<<"$input" 2>/dev/null || true)
    # A push or a new PR, at the start of the command or after ; & | ( or space.
    trigger='(^|[[:space:];&|(])(git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+push|gh[[:space:]]+pr[[:space:]]+create)([[:space:];&|)]|$)'
    if [[ $cmd =~ $trigger ]]; then
        [[ -n ${PRECHECK_MATCH_ONLY:-} ]] && { echo match; exit 0; }
    else
        [[ -n ${PRECHECK_MATCH_ONLY:-} ]] && echo skip
        exit 0
    fi
fi

# Run through mise when it's there, so the pinned toolchains are used.
# shellcheck disable=SC2329  # called through step
run() { if command -v mise >/dev/null; then mise exec -- "$@"; else "$@"; fi; }

if ((all)); then
    changed=$(git ls-files)
else
    if ! mb=$(git merge-base "$base" HEAD 2>/dev/null); then
        echo "precheck: can't find $base; run git fetch origin main" >&2
        ((hook)) && exit 0
        exit 1
    fi
    changed=$( { git diff --name-only --no-renames "$mb"; git ls-files --others --exclude-standard; } | sort -u)
fi
has() { grep -Eq "$1" <<<"$changed"; }

failed=()
step() {
    local name=$1; shift
    echo "precheck: $name" >&2
    if ! "$@" >"$tmp/out" 2>&1; then
        failed+=("$name")
        tail -n 40 "$tmp/out" >&2
    fi
}
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

if has '^(runtime/|tools/.*\.py$|pyproject\.toml$|uv\.lock$)'; then
    step "ruff check" run uv run ruff check runtime tools
    step "ruff format" run uv run ruff format --check
    step "pytest" run uv run pytest -q -x
    if has '^(runtime/src/reachy_bridge_runtime/upstream\.py|uv\.lock|runtime/pyproject\.toml)$'; then
        step "upstream seam" run uv run reachy-bridge-runtime --check
    fi
fi
if has '^web/'; then
    step "web typecheck" run bash -c 'cd web && bun run typecheck'
    step "web tests" run bash -c 'cd web && bun test'
fi
if has '^(controller/|web/)'; then
    step "cargo fmt" run bash -c 'cd controller && cargo fmt --check'
    step "clippy" run bash -c 'cd controller && cargo clippy --locked --all-targets -- -D warnings'
    step "cargo test" run bash -c 'cd controller && cargo test --locked -q'
fi
if has '^(deploy/|tools/.*\.sh$)'; then
    mapfile -t scripts < <(git ls-files 'deploy/*.sh' deploy/reachy-bridge 'tools/*.sh')
    step "shellcheck" run shellcheck "${scripts[@]}"
    has '^deploy/' && step "test_cli" bash tools/test_cli.sh
    has '^deploy/' && step "test_uninstall" bash tools/test_uninstall.sh
    has '^tools/(test_)?review_gate\.sh$' && step "test_review_gate" bash tools/test_review_gate.sh
    has '^tools/(test_)?catchup_check\.sh$' && step "test_catchup_check" bash tools/test_catchup_check.sh
    has '^tools/(test_)?precheck\.sh$' && step "test_precheck" bash tools/test_precheck.sh
    has '^tools/(test_)?hardware_qa_gate\.sh$' && step "test_hardware_qa_gate" bash tools/test_hardware_qa_gate.sh
fi

gitdir=$(git rev-parse --git-dir)
counter="$gitdir/precheck-blocks-$(tr -cd "A-Za-z0-9_-" <<<"${session:-none}")"
if ((${#failed[@]} == 0)); then
    echo "precheck: passed" >&2
    # A pass starts the session's block count again.
    ((hook)) && rm -f "$counter"
    exit 0
fi
summary="precheck failed: ${failed[*]}"
if ! ((hook)); then
    echo "$summary" >&2
    exit 1
fi

echo "$(date -u +%FT%TZ) mode=$mode session=${session:-?} head=$(git rev-parse --short HEAD) $summary" >>"$gitdir/precheck.log"
[[ $mode == enforce ]] || exit 0
blocks=$(cat "$counter" 2>/dev/null || echo 0)
if ((blocks >= 2)); then
    echo "precheck: letting this through after two blocks; say in the PR what still fails and why" >&2
    exit 0
fi
echo $((blocks + 1)) >"$counter"
echo "$summary. Fix it and try again (bash tools/precheck.sh). After three failed fix rounds on the same check, stop and ask the owner." >&2
exit 2
