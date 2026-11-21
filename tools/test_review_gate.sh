#!/usr/bin/env bash
# Tests tools/review_gate.sh (#302) against a stub `gh` that answers from
# fixture files and logs the status it would set. Changes nothing outside a
# temp directory and makes no network calls.
#
#   bash tools/test_review_gate.sh
set -euo pipefail

GATE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/review_gate.sh
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
fails=0
HEAD=0123456789abcdef0123456789abcdef01234567
OLD=fedcba9876543210fedcba9876543210fedcba98

mkdir -p "$ROOT/bin"
# The stub serves $ROOT/fx/{pr,files,comments,commits,shared}.json by endpoint
# (pr-<n>.json for PR n when it exists), fails an endpoint when
# $ROOT/fx/fail-<name> exists (fail-statuses-<k>: only the next k status
# POSTs), and logs a status POST as "<sha> <state> <description>".
cat >"$ROOT/bin/gh" <<'EOF'
#!/usr/bin/env bash
args=("$@")
for a in "${args[@]}"; do
    case $a in
        repos/*/statuses/*)
            [[ -e $FX/fail-statuses ]] && exit 1
            for f in "$FX"/fail-statuses-*; do
                [[ -e $f ]] || continue
                k=${f##*-}; rm "$f"; ((k > 1)) && touch "$FX/fail-statuses-$((k - 1))"; exit 1
            done
            sha=${a##*/} state= desc=
            for f in "${args[@]}"; do
                case $f in state=*) state=${f#state=} ;; description=*) desc=${f#description=} ;; esac
            done
            echo "$sha $state $desc" >>"$FX/status.log"
            exit 0 ;;
        repos/*/commits/*/pulls) [[ -e $FX/fail-shared ]] && exit 1; cat "$FX/shared.json"; exit 0 ;;
        repos/*/pulls/*/files) [[ -e $FX/fail-files ]] && exit 1; cat "$FX/files.json"; exit 0 ;;
        repos/*/pulls/*/commits) [[ -e $FX/fail-commits ]] && exit 1; cat "$FX/commits.json"; exit 0 ;;
        repos/*/issues/*/comments) [[ -e $FX/fail-comments ]] && exit 1; cat "$FX/comments.json"; exit 0 ;;
        repos/*/pulls/*) n=${a##*/}; if [[ -e $FX/pr-$n.json ]]; then cat "$FX/pr-$n.json"; else cat "$FX/pr.json"; fi; exit 0 ;;
    esac
done
echo "stub gh: unexpected call: $*" >&2
exit 1
EOF
chmod +x "$ROOT/bin/gh"
export PATH="$ROOT/bin:$PATH" FX="$ROOT/fx" GH_REPO=owner/repo PR=7 REVIEW_GATE_RETRY_DELAY=0

# setup [author] [labels-json]: a fresh open PR by `author` with a plain file.
setup() {
    rm -rf "$FX"
    mkdir -p "$FX"
    jq -n --arg sha "$HEAD" --arg user "${1:-zeapsu}" --argjson labels "${2:-[]}" \
        '{state: "open", head: {sha: $sha}, html_url: "https://example.invalid/pr/7",
          user: {login: $user}, labels: [$labels[] | {name: .}], changed_files: 1,
          base: {ref: "main", repo: {default_branch: "main"}}}' >"$FX/pr.json"
    files web/src/App.tsx
    echo '[]' >"$FX/comments.json"
    echo '[]' >"$FX/commits.json"
    jq -n --arg h "$HEAD" '[{number: 7, state: "open", base: {ref: "main"}, head: {sha: $h}}]' >"$FX/shared.json"
}
files() {
    printf '%s\n' "$@" | jq -R '{filename: .}' | jq -s . >"$FX/files.json"
    jq --argjson n $# '.changed_files = $n' "$FX/pr.json" >"$FX/p" && mv "$FX/p" "$FX/pr.json"
}
# comment ASSOCIATION BODY [EDITED]: appends a comment (BODY may hold \n and \r).
comment() {
    jq --arg a "$1" --arg b "$(printf '%b' "$2")" --arg u "${3:-2026-01-01T00:00:00Z}" \
        '. + [{author_association: $a, body: $b, created_at: "2026-01-01T00:00:00Z", updated_at: $u}]' \
        "$FX/comments.json" >"$FX/c" && mv "$FX/c" "$FX/comments.json"
}
# expect NAME STATE [TEXT]: the gate runs, sets STATE on HEAD, saying TEXT.
expect() {
    local name=$1 state=$2 text=${3:-}
    rm -f "$FX/status.log"
    if ! bash "$GATE" >"$FX/out" 2>&1; then fail "$name: gate failed"; cat "$FX/out"; return; fi
    local got
    got=$(cat "$FX/status.log" 2>/dev/null || true)
    if [[ $(grep -c . <<<"$got") != 1 ]]; then fail "$name: want one status, got '$got'"; cat "$FX/out"
    elif [[ $got != "$HEAD $state"* ]]; then fail "$name: want $state, got '$got'"; cat "$FX/out"
    elif [[ -n $text && $got != *"$text"* ]]; then fail "$name: '$got' doesn't say '$text'"
    else echo "ok: $name"; fi
}
fail() { echo "FAIL: $*"; fails=$((fails + 1)); }

# broken NAME: the gate exits non-zero and leaves an `error` status on HEAD
# (when it got as far as knowing HEAD).
broken() {
    rm -f "$FX/status.log"
    if bash "$GATE" >"$FX/out" 2>&1; then fail "$1: gate succeeded"; cat "$FX/out"; return; fi
    local got
    got=$(tail -n1 "$FX/status.log" 2>/dev/null || true)
    if [[ $got == "$HEAD error"* ]]; then echo "ok: $1"; else fail "$1: last status '$got'"; cat "$FX/out"; fi
}

setup
expect "no marker: pending" pending "Needs Review clean"

setup; comment OWNER "Review: clean @$HEAD"
expect "owner's marker on the head" success "Review: clean at 0123456"

setup; comment OWNER "\r\nReview: clean @$HEAD  \r\n"
expect "marker alone with CRLF and spaces around it" success

setup; comment OWNER "Looks good.\nReview: clean @$HEAD"
expect "marker with other lines in the comment" pending

setup; comment OWNER "Not clean yet. Once fixed, post:\n\`\`\`\nReview: clean @$HEAD\n\`\`\`"
expect "quoted marker template" pending

setup; comment OWNER "Review: clean @$OLD"
expect "marker on an older head" pending

setup; comment OWNER "Review: clean @${HEAD:0:12}"
expect "short sha isn't enough" pending

setup; comment OWNER "Review: clean @$HEAD except one high finding"
expect "marker with text after it" pending

setup; comment OWNER "Done. Review: clean @$HEAD"
expect "marker not at a line start" pending

setup; comment OWNER "review: clean @$HEAD"
expect "wrong case" pending

setup; comment OWNER "Review: clean @$HEAD" 2026-01-02T00:00:00Z
expect "edited comment doesn't count" pending

FOOT='\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_'
setup; comment OWNER "Review: clean @$HEAD$FOOT"
expect "marker with the Claude Code footer" success

setup; comment OWNER "Review: clean @$HEAD\r\n\r\n---\r\n_Generated by [Claude Code](https://claude.ai/code)_\n"
expect "footer with CRLF line ends" success

setup; comment OWNER "Review: clean @$HEAD\nlooks fine$FOOT"
expect "other text before the footer" pending

setup; comment OWNER "Review: clean @$HEAD$FOOT\nmore"
expect "text after the footer" pending

setup; comment OWNER "Review: clean @$HEAD\n\n---\n_Generated by [Claude Code](https://example.com)_"
expect "a different footer" pending

setup; comment OWNER "Review: clean @$HEAD\n\n---\n_Generated by Claude Code_"
expect "a footer without the link" pending

setup; comment OWNER "Review: clean @$HEAD$FOOT\nReview: clean @$HEAD$FOOT"
expect "two markers with footers in one comment" pending

setup; comment OWNER "Review: clean @$HEAD\n---\n_Generated by [Claude Code](https://claude.ai/code)_"
expect "footer without a blank line before it" pending

setup; comment OWNER "Review: cl\rean @$HEAD$FOOT"
expect "a stray CR inside the marker" pending

# Both scripts must strip the same footer.
if [[ $(grep -h "^FOOTER=" tools/review_gate.sh) == "$(grep -h "^FOOTER=" tools/catchup_check.sh)" ]]; then
    echo "ok: review_gate.sh and catchup_check.sh agree on the footer"
else
    echo "FAIL: review_gate.sh and catchup_check.sh strip different footers"; fails=$((fails + 1))
fi

for who in NONE CONTRIBUTOR FIRST_TIME_CONTRIBUTOR MEMBER COLLABORATOR; do
    setup; comment "$who" "Review: clean @$HEAD"
    expect "marker from $who doesn't count" pending
done

setup; printf '[{"author_association":"NONE","body":"hi","created_at":"x","updated_at":"x"}][{"author_association":"OWNER","body":"Review: clean @%s","created_at":"x","updated_at":"x"}]' "$HEAD" >"$FX/comments.json"
expect "marker on a second page" success

setup "zeapsu" '["security"]'; comment OWNER "Review: clean @$HEAD"
expect "security label needs both extra markers" pending "Needs Security review, Second review"

setup "zeapsu" '["Auth"]'; comment OWNER "Review: clean @$HEAD"
expect "auth label, any case" pending "security PR: label"

for f in controller/src/auth.rs controller/src/profiles.rs controller/src/main.rs \
    controller/src/lib.rs controller/src/control.rs controller/src/audio.rs \
    runtime/src/reachy_bridge_runtime/features/app_bind_shim.py \
    runtime/src/reachy_bridge_runtime/audio_policy.py runtime/src/reachy_bridge_runtime/main.py \
    web/src/api/auth.ts deploy/reachy-bridge deploy/install.sh \
    deploy/systemd/reachy-bridge-controller.service .github/workflows/ci.yml \
    .github/dependabot.yml CONTRIBUTING.md controller/src/logs.rs controller/src/apps.rs \
    runtime/src/reachy_bridge_runtime/features/sim_sounds.py mise.toml tools/build_webrtcsink_jetson.sh \
    controller/build.rs web/vite.config.ts .claude/settings.json .mcp.json tools/review_gate.sh tools/test_review_gate.sh \
    controller/src/sim.rs controller/src/setup.rs controller/src/app_proxy.rs controller/src/state.rs \
    controller/src/routes/store.rs controller/src/routes/sounds.rs \
    runtime/src/reachy_bridge_runtime/audio_gateway.py runtime/src/reachy_bridge_runtime/features/sim_apps.py \
    web/src/logic/auth.ts web/src/api/hf.ts web/src/api/client.ts web/src/components/Login.tsx \
    deploy/uninstall.sh controller/Cargo.lock runtime/pyproject.toml uv.lock web/bun.lock AGENTS.md \
    REVIEW.md docs/reliability-checklist.md tools/checklist.py tools/precheck.sh tools/test_precheck.sh \
    .gitignore web/CLAUDE.md controller/src/AGENTS.md web/.claude/skills/x/SKILL.md runtime/.mcp.json \
    .gitattributes controller/.gitattributes CLAUDE.local.md web/CLAUDE.local.md GEMINI.md AGENTS.override.md \
    .cursor/rules/x.mdc .cursorrules .codex/config.toml .gemini/settings.json .windsurf/rules/x.md \
    .windsurfrules .clinerules .clinerules/x.md .continue/config.yaml .roo/rules/x.md .aider.conf.yml \
    web/.cursor/rules/x.mdc .gitmodules tools/udev/99-reachy-mini-doa.rules tools/udev/new.rules \
    runtime/src/reachy_bridge_runtime/features/apps_venv_sync.py runtime/src/reachy_bridge_runtime/upstream.py \
    controller/src/host.rs controller/src/routes/apps.rs controller/src/routes/power.rs \
    controller/src/routes/logs.rs; do
    setup; files web/src/App.tsx "$f"; comment OWNER "Review: clean @$HEAD"
    expect "security path $f" pending "security PR: paths"
done

setup; files controller/src/routes/powers.rs runtime/src/reachy_bridge_runtime/audio_tee.py \
    web/src/api/power.ts tools/review_gate.sh.bak deploy/systemd/README.md \
    docs/REVIEW.md docs/reliability-checklist.md.bak web/.gitignore docs/AGENTS.md.txt docs/claude/notes.md web/mcp.json \
    docs/CLAUDE.local.md.txt docs/cursor/notes.md tools/udev/README.md tools/udev/sub/x.rules \
    runtime/src/reachy_bridge_runtime/upstream_notes.py controller/src/hosts.rs docs/gitmodules.md
comment OWNER "Review: clean @$HEAD"
expect "near-miss paths aren't security paths" success

setup; jq -n '[{filename: "controller/src/login.rs", previous_filename: "controller/src/auth.rs"}]' >"$FX/files.json"
comment OWNER "Review: clean @$HEAD"
expect "renamed away from a security path" pending "security PR: paths"

setup; jq '.changed_files = 3001' "$FX/pr.json" >"$FX/p" && mv "$FX/p" "$FX/pr.json"
comment OWNER "Review: clean @$HEAD"
expect "more files than the listing shows" pending "unlisted files"

setup "zeapsu" '["security"]'; files controller/src/auth.rs
comment OWNER "Review: clean @$HEAD\nSecurity review: clean @$HEAD\nSecond review: clean @$HEAD"
expect "all three markers in one comment" pending

setup "zeapsu" '["security"]'; files controller/src/auth.rs
comment OWNER "Review: clean @$HEAD"; comment OWNER "Security review: clean @$HEAD"
comment OWNER "Second review: clean @$HEAD"
expect "all three markers" success "Review, Security review, Second review: clean at 0123456 (security PR: label, paths)"

setup "zeapsu"; files controller/src/auth.rs
comment OWNER "Review: clean @$HEAD"; comment OWNER "Security review: clean @$HEAD"
comment OWNER "Second review: clean @$OLD"
expect "second review of an older head" pending "Needs Second review"

# bump TYPE... : Dependabot's commit, one dependency per update type given,
# plus the updater's merge of main.
bump() {
    local msg="Bump things"$'\n\n---\nupdated-dependencies:\n' t
    for t in "$@"; do
        msg+="- dependency-name: dep-$t"$'\n'"  dependency-version: 1.0.0"$'\n'"  update-type: version-update:semver-$t"$'\n'
    done
    msg+=$'...\n\nSigned-off-by: dependabot[bot] <support@github.com>'
    jq -n --arg m "$msg" '[{sha: "a1", committer: {login: "web-flow"}, commit: {verification: {verified: true}, message: $m}, author: {login: "dependabot[bot]"}, parents: [1]},
        {sha: "a2", committer: {login: "web-flow"}, commit: {verification: {verified: true}, message: "Merge main"}, author: {login: "github-actions[bot]"}, parents: [1, 2]}]' >"$FX/commits.json"
}

setup "dependabot[bot]"; files uv.lock; bump patch minor
expect "Dependabot patch and minor updates pass" success "Dependabot patch or minor update"

setup "dependabot[bot]"; files uv.lock; bump minor major
expect "Dependabot major update needs a Review marker" pending "Needs Review clean @<full head sha> (Dependabot: major or unclassified update)"

setup "dependabot[bot]"; files uv.lock; bump major; comment OWNER "Review: clean @$HEAD"
expect "Dependabot major with a Review marker passes, security paths aside" success

setup "dependabot[bot]" '["security"]'; files uv.lock; bump major; comment OWNER "Review: clean @$HEAD"
expect "Dependabot major with the security label needs all three" pending "Needs Security review, Second review"

setup "dependabot[bot]"; files .github/workflows/ci.yml; bump patch
expect "Dependabot workflow bump needs a Review marker" pending "(Dependabot: changes .github/)"

setup "dependabot[bot]"; files uv.lock
jq -n '[{sha: "a1", committer: {login: "web-flow"}, commit: {verification: {verified: true}, message: "Bump things"}, author: {login: "dependabot[bot]"}, parents: [1]}]' >"$FX/commits.json"
expect "Dependabot update with no update list needs a Review marker" pending "major or unclassified"

setup "dependabot[bot]"; files uv.lock; bump patch
jq '.[0].commit.message |= sub("\\.\\.\\."; "- dependency-name: dep-x\n  dependency-version: 2.0.0\n...")' "$FX/commits.json" >"$FX/c" && mv "$FX/c" "$FX/commits.json"
expect "a dependency with no update type of its own needs a Review marker" pending

setup "dependabot[bot]"; files uv.lock; bump major
jq '.[0].commit.message |= "  update-type: version-update:semver-patch\n" + .' "$FX/commits.json" >"$FX/c" && mv "$FX/c" "$FX/commits.json"
expect "an update-type line outside the update list doesn't count" pending "major or unclassified"

setup "dependabot[bot]"; files uv.lock; bump patch
jq '[.[0], (.[0] | .sha = "a3" | .commit.message = "x\n---\nupdated-dependencies:\n- dependency-name: y\n  update-type: version-update:semver-patch\n...")] + .[1:]' "$FX/commits.json" >"$FX/c" && mv "$FX/c" "$FX/commits.json"
expect "two Dependabot commits need a Review marker" pending

setup "dependabot[bot]" '["security"]'; files uv.lock; bump patch
expect "a Dependabot patch with the security label needs all three" pending "Needs Review, Security review, Second review"

setup "dependabot[bot]"; files uv.lock; bump patch
jq '.changed_files = 3001' "$FX/pr.json" >"$FX/p" && mv "$FX/p" "$FX/pr.json"
expect "a Dependabot PR with unlisted files needs a Review marker" pending "more files than the listing shows"

setup "dependabot[bot]"
jq -n '[{sha: "a1", committer: {login: "web-flow"}, commit: {verification: {verified: true}}, author: {login: "dependabot[bot]"}, parents: [1]},
        {sha: "b2", committer: {login: "zeapsu"}, commit: {verification: {verified: false}}, author: {login: "zeapsu"}, parents: [1]}]' >"$FX/commits.json"
expect "Dependabot PR with someone else's commit needs review" pending

setup "dependabot[bot]"
jq -n '[{sha: "a2", committer: {login: "web-flow"}, commit: {verification: {verified: true}}, author: {login: "github-actions[bot]"}, parents: [1]}]' >"$FX/commits.json"
expect "workflow commit that isn't a merge needs review" pending

setup "dependabot-impostor"
expect "only Dependabot itself skips review" pending

setup; comment OWNER "Review: clean @$HEAD"
jq '.base.ref = "feature"' "$FX/pr.json" >"$FX/p" && mv "$FX/p" "$FX/pr.json"
rm -f "$FX/status.log"
if bash "$GATE" >"$FX/out" 2>&1 && [[ ! -e $FX/status.log ]]; then echo "ok: PR into another branch: no status"
else fail "PR into another branch: set a status or failed"; fi

setup; comment OWNER "Review: clean @$HEAD"
jq -n --arg h "$HEAD" --arg o "$OLD" '[{number: 7, state: "open", base: {ref: "main"}, head: {sha: $h}},
    {number: 9, state: "open", base: {ref: "main"}, head: {sha: $h}}, {number: 4, state: "closed", base: {ref: "main"}, head: {sha: $h}},
    {number: 5, state: "open", base: {ref: "feature"}, head: {sha: $h}}, {number: 8, state: "open", base: {ref: "main"}, head: {sha: $o}}]' >"$FX/shared.json"
expect "head shared with another open PR into main" pending "Head is also the head of #9 -"

setup; comment OWNER "Review: clean @$HEAD"
jq -n --arg h "$HEAD" --arg o "$OLD" '[{number: 7, state: "open", base: {ref: "main"}, head: {sha: $h}},
    {number: 8, state: "open", base: {ref: "main"}, head: {sha: $o}}]' >"$FX/shared.json"
expect "a stacked PR that only contains the head doesn't block it" success

setup; comment OWNER "Review: clean @${HEAD:0:7}"; comment OWNER "Review: clean @$HEAD\nnotes"
expect "near misses: pending" pending
if grep -q "(another commit)" "$FX/out" && grep -q "other text in the comment" "$FX/out"; then echo "ok: near misses explained in the log"
else fail "near misses not explained"; cat "$FX/out"; fi

# A failing API call fails the run and replaces any earlier pass.
setup; files controller/src/auth.rs; comment OWNER "Review: clean @$HEAD"; touch "$FX/fail-files"
broken "files listing fails: error, not a one-marker pass"
setup; comment OWNER "Review: clean @$HEAD"; touch "$FX/fail-comments"
broken "comments listing fails: error"
setup "dependabot[bot]"; touch "$FX/fail-commits"
broken "Dependabot commits listing fails: error"
setup; touch "$FX/fail-shared"
broken "shared-head listing fails: error"
setup; comment OWNER "Review: clean @$HEAD"; touch "$FX/fail-statuses"
rm -f "$FX/status.log"
if bash "$GATE" >"$FX/out" 2>&1; then fail "status POST fails: gate succeeded"
else echo "ok: status POST fails: run fails"; fi

setup; comment OWNER "Review: clean @$HEAD"; touch "$FX/fail-statuses-2"
expect "status POST fails twice, then lands" success

setup; files web/src/App.tsx $'.github/x\r\e[2K::warning::spoofed'
comment OWNER "Review: clean @$HEAD"
expect "control characters in a file name" pending
if grep -q $'[\r\e]' "$FX/out"; then fail "control characters reached the log"; cat -v "$FX/out"
else echo "ok: no control characters in the log"; fi

setup; jq '.state = "closed"' "$FX/pr.json" >"$FX/p" && mv "$FX/p" "$FX/pr.json"
rm -f "$FX/status.log"
if bash "$GATE" >"$FX/out" 2>&1 && [[ ! -e $FX/status.log ]]; then echo "ok: closed PR: no status"
else fail "closed PR: set a status or failed"; fi

# A closed fork PR (#7 here) that shared the head of open PR #9 re-checks #9,
# which then passes on its own markers; the re-check doesn't recurse.
setup; comment OWNER "Review: clean @$HEAD"
jq '.state = "closed"' "$FX/pr.json" >"$FX/pr-7.json"
jq '.state = "open"' "$FX/pr.json" >"$FX/pr-9.json"
jq -n --arg h "$HEAD" '[{number: 7, state: "closed", base: {ref: "main"}, head: {sha: $h}},
    {number: 9, state: "open", base: {ref: "main"}, head: {sha: $h}}]' >"$FX/shared.json"
expect "closing a PR re-checks the open PR with the same head" success
jq '.base.ref = "feature" | .state = "open"' "$FX/pr.json" >"$FX/pr-7.json"
jq -n --arg h "$HEAD" '[{number: 7, state: "open", base: {ref: "feature"}, head: {sha: $h}},
    {number: 9, state: "open", base: {ref: "main"}, head: {sha: $h}}]' >"$FX/shared.json"
expect "retargeting a PR away from main re-checks the PR with the same head" success
jq '.state = "closed"' "$FX/pr.json" >"$FX/pr-7.json"
cp "$FX/pr-7.json" "$FX/pr-9.json"
jq -n --arg h "$HEAD" '[{number: 7, state: "closed", base: {ref: "main"}, head: {sha: $h}},
    {number: 9, state: "open", base: {ref: "main"}, head: {sha: $h}}]' >"$FX/shared.json"
rm -f "$FX/status.log"
if bash "$GATE" >"$FX/out" 2>&1 && [[ ! -e $FX/status.log ]] && [[ $(grep -c "Re-checking" "$FX/out") == 1 ]]; then
    echo "ok: a re-check doesn't recurse"
else fail "a re-check recursed or failed"; cat "$FX/out"; fi

for bad in "7; true" 07 0 ""; do
    if PR=$bad bash "$GATE" >"$FX/out" 2>&1; then fail "PR '$bad' accepted"
    else echo "ok: PR '$bad' refused"; fi
done

echo
if ((fails)); then echo "$fails failed"; exit 1; fi
echo "all passed"
