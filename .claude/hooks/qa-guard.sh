#!/usr/bin/env bash
# Claude Code PreToolUse hook (Bash): blocks gh commands that add the
# `qa: passed` label or post a `hardware-qa` status. Only the owner adds that
# label, after testing the head on the robot, and only the Hardware QA gate
# posts that status (AGENTS.md, merge gate 6); agents act with the owner's
# account, so GitHub can't tell them apart. A guard against slips, not a
# security boundary: tools/hardware_qa_gate.sh is what the merge rests on.
#
# Reads the hook's JSON on stdin; exit 2 blocks the command and tells the
# agent why.
input=$(cat)
cmd=$(jq -r '.tool_input.command // empty' <<<"$input" 2>/dev/null) || cmd=$input
# Only gh where a command starts (a line, or after ; & | ( ` or $( ), so
# prose that mentions gh, a commit message say, passes.
grep -qE '(^|[;&|(`]|\$\()[[:space:]]*gh[[:space:]]' <<<"$cmd" || exit 0

block() {
    echo "Blocked: this $1. Only the owner adds 'qa: passed', and only the Hardware QA gate" \
        "posts the hardware-qa status (AGENTS.md, merge gate 6)." >&2
    exit 2
}
# A gh api call that writes: an explicit method or any field (gh api then
# defaults to POST).
writes() {
    grep -qE -- '(-X|--method)[[:space:]=]*["'\'']?(POST|PUT|PATCH)|(^|[[:space:]])(-[fF]|--field|--raw-field|--input)([[:space:]=]|$)' <<<"$cmd"
}
if grep -qiE 'qa(:|%3a)([[:space:]]|%20|\+)*passed' <<<"$cmd"; then
    grep -qE -- '--add-label' <<<"$cmd" && block "adds the 'qa: passed' label"
    grep -qE '(pr|issue)[[:space:]]+create' <<<"$cmd" \
        && grep -qE -- '(^|[[:space:]])(--label|-l)([[:space:]=]|$)' <<<"$cmd" \
        && block "adds the 'qa: passed' label"
    grep -qE '/labels' <<<"$cmd" && writes && block "adds the 'qa: passed' label"
fi
if grep -qE 'statuses' <<<"$cmd" && grep -qE 'hardware-qa' <<<"$cmd" && writes; then
    block "posts a hardware-qa status"
fi
exit 0
