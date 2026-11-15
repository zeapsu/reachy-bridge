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
# The stub serves $ROOT/fx/{pr,files,comments,commits}.json by endpoint and
# logs a status POST as "<sha> <state> <description>".
cat >"$ROOT/bin/gh" <<'EOF'
#!/usr/bin/env bash
args=("$@")
for a in "${args[@]}"; do
    case $a in
        repos/*/statuses/*)
            sha=${a##*/} state= desc=
            for f in "${args[@]}"; do
                case $f in state=*) state=${f#state=} ;; description=*) desc=${f#description=} ;; esac
            done
            echo "$sha $state $desc" >>"$FX/status.log"
            exit 0 ;;
        repos/*/pulls/*/files) cat "$FX/files.json"; exit 0 ;;
        repos/*/pulls/*/commits) cat "$FX/commits.json"; exit 0 ;;
        repos/*/issues/*/comments) cat "$FX/comments.json"; exit 0 ;;
        repos/*/pulls/*) cat "$FX/pr.json"; exit 0 ;;
    esac
done
echo "stub gh: unexpected call: $*" >&2
exit 1
EOF
chmod +x "$ROOT/bin/gh"
export PATH="$ROOT/bin:$PATH" FX="$ROOT/fx" GH_REPO=owner/repo PR=7

# setup [author] [labels-json]: a fresh open PR by `author` with a plain file.
setup() {
    rm -rf "$FX"
    mkdir -p "$FX"
    jq -n --arg sha "$HEAD" --arg user "${1:-zeapsu}" --argjson labels "${2:-[]}" \
        '{state: "open", head: {sha: $sha}, html_url: "https://example.invalid/pr/7",
          user: {login: $user}, labels: [$labels[] | {name: .}]}' >"$FX/pr.json"
    files web/src/App.tsx
    echo '[]' >"$FX/comments.json"
    echo '[]' >"$FX/commits.json"
}
files() { printf '%s\n' "$@" | jq -R '{filename: .}' | jq -s . >"$FX/files.json"; }
# comment ASSOCIATION BODY: appends a comment (BODY may hold \n and \r).
comment() {
    jq --arg a "$1" --arg b "$(printf '%b' "$2")" '. + [{author_association: $a, body: $b}]' \
        "$FX/comments.json" >"$FX/c" && mv "$FX/c" "$FX/comments.json"
}
# expect NAME STATE [TEXT]: the gate runs, sets STATE on HEAD, saying TEXT.
expect() {
    local name=$1 state=$2 text=${3:-}
    rm -f "$FX/status.log"
    if ! bash "$GATE" >"$FX/out" 2>&1; then fail "$name: gate failed"; cat "$FX/out"; return; fi
    local got
    got=$(cat "$FX/status.log" 2>/dev/null || true)
    if [[ $got != "$HEAD $state"* ]]; then fail "$name: want $state, got '$got'"; cat "$FX/out"
    elif [[ -n $text && $got != *"$text"* ]]; then fail "$name: '$got' doesn't say '$text'"
    else echo "ok: $name"; fi
}
fail() { echo "FAIL: $*"; fails=$((fails + 1)); }

setup
expect "no marker: pending" pending "Needs Review clean @0123456"

setup; comment OWNER "Review: clean @${HEAD:0:7}"
expect "owner's marker on the head" success "Review: clean at 0123456"

setup; comment OWNER "Looks good.\r\nReview: clean @$HEAD\r\nThanks"
expect "marker on its own line among others, CRLF" success

setup; comment OWNER "Review: clean @${OLD:0:7}"
expect "marker on an older head" pending

setup; comment OWNER "Review: clean @${HEAD:0:6}"
expect "prefix shorter than 7" pending

setup; comment OWNER "Done. Review: clean @${HEAD:0:7}"
expect "marker not at a line start" pending

setup; comment OWNER "review: clean @${HEAD:0:7}"
expect "wrong case" pending

for who in NONE CONTRIBUTOR FIRST_TIME_CONTRIBUTOR MEMBER COLLABORATOR; do
    setup; comment "$who" "Review: clean @${HEAD:0:7}"
    expect "marker from $who doesn't count" pending
done

setup "zeapsu" '["security"]'; comment OWNER "Review: clean @${HEAD:0:7}"
expect "security label needs both extra markers" pending "Needs Security review, Second review"

setup "zeapsu" '["Auth"]'; comment OWNER "Review: clean @${HEAD:0:7}"
expect "auth label, any case" pending "security PR: label"

for f in controller/src/auth.rs controller/src/profiles.rs controller/src/main.rs \
    runtime/src/reachy_bridge_runtime/features/app_bind_shim.py \
    runtime/src/reachy_bridge_runtime/audio_policy.py deploy/reachy-bridge \
    deploy/systemd/reachy-bridge-controller.service .github/workflows/ci.yml \
    tools/review_gate.sh; do
    setup; files web/src/App.tsx "$f"; comment OWNER "Review: clean @${HEAD:0:7}"
    expect "security path $f" pending "security PR: paths"
done

setup; files controller/src/routes/power.rs runtime/src/reachy_bridge_runtime/audio_tee.py \
    deploy/install.sh tools/review_gate.sh.bak .github/dependabot.yml
comment OWNER "Review: clean @${HEAD:0:7}"
expect "near-miss paths aren't security paths" success

setup; jq '[{filename: "controller/src/login.rs", previous_filename: "controller/src/auth.rs"}]' -n >"$FX/files.json"
comment OWNER "Review: clean @${HEAD:0:7}"
expect "renamed away from a security path" pending "security PR: paths"

setup "zeapsu" '["security"]'; files controller/src/auth.rs
comment OWNER "Review: clean @${HEAD:0:7}\nSecurity review: clean @${HEAD:0:7}"
comment OWNER "Second review: clean @$HEAD"
expect "all three markers" success "Review, Security review, Second review: clean at 0123456 (security PR: label, paths)"

setup "zeapsu"; files controller/src/auth.rs
comment OWNER "Review: clean @${HEAD:0:7}\nSecurity review: clean @${HEAD:0:7}\nSecond review: clean @${OLD:0:7}"
expect "second review of an older head" pending "Needs Second review"

setup "dependabot[bot]"
jq -n '[{sha: "a1", committer: {login: "web-flow"}, commit: {verification: {verified: true}}, author: {login: "dependabot[bot]"}, parents: [1]},
        {sha: "a2", committer: {login: "web-flow"}, commit: {verification: {verified: true}}, author: {login: "github-actions[bot]"}, parents: [1, 2]}]' >"$FX/commits.json"
expect "Dependabot's own commits pass" success "Dependabot update"

setup "dependabot[bot]"
jq -n '[{sha: "a1", committer: {login: "web-flow"}, commit: {verification: {verified: true}}, author: {login: "dependabot[bot]"}, parents: [1]},
        {sha: "b2", committer: {login: "zeapsu"}, commit: {verification: {verified: false}}, author: {login: "zeapsu"}, parents: [1]}]' >"$FX/commits.json"
expect "Dependabot PR with someone else's commit needs review" pending

setup "dependabot[bot]"
jq -n '[{sha: "a2", committer: {login: "web-flow"}, commit: {verification: {verified: true}}, author: {login: "github-actions[bot]"}, parents: [1]}]' >"$FX/commits.json"
expect "workflow commit that isn't a merge needs review" pending

setup "dependabot-impostor"
expect "only Dependabot itself skips review" pending

setup; jq '.state = "closed"' "$FX/pr.json" >"$FX/p" && mv "$FX/p" "$FX/pr.json"
rm -f "$FX/status.log"
if bash "$GATE" >"$FX/out" 2>&1 && [[ ! -e $FX/status.log ]]; then echo "ok: closed PR: no status"
else fail "closed PR: set a status or failed"; fi

if PR="7; true" bash "$GATE" >"$FX/out" 2>&1; then fail "non-numeric PR accepted"
else echo "ok: non-numeric PR refused"; fi

echo
if ((fails)); then echo "$fails failed"; exit 1; fi
echo "all passed"
