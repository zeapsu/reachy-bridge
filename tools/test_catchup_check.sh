#!/usr/bin/env bash
# Tests tools/catchup_check.sh against real git histories in a temp
# directory and a stub `gh` that serves the PR and its comments and logs the
# comments it would post. No network calls.
#
#   bash tools/test_catchup_check.sh
set -euo pipefail

TOOLS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CHECK=$TOOLS/catchup_check.sh
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
fails=0
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.com GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.com
export GIT_CONFIG_NOSYSTEM=1 HOME=$ROOT

mkdir -p "$ROOT/bin"
cat >"$ROOT/bin/gh" <<'EOF'
#!/usr/bin/env bash
case "$*" in
    "api user -q .login") echo "${LOGIN:-owner}" ;;
    "api repos/owner/repo/pulls/1") cat "$FX/pr.json" ;;
    "api --paginate repos/owner/repo/issues/1/comments") cat "$FX/comments.json" ;;
    "api --paginate repos/owner/repo/issues/1/timeline")
        [[ -e $FX/fail-timeline ]] && { echo '[]'; exit 1; }
        cat "$FX/timeline.json" 2>/dev/null || echo '[]' ;;
    "api --paginate repos/owner/repo/issues/1/events")
        [[ -e $FX/fail-events ]] && { echo '[]'; exit 1; }
        cat "$FX/events.json" 2>/dev/null || echo '[]' ;;
    "api -X POST repos/owner/repo/issues/1/comments -f body="*) all="$*"; echo "${all#*body=}" >>"$FX/posted.log" ;;
    *) echo "stub gh: unexpected call: $*" >&2; exit 1 ;;
esac
EOF
chmod +x "$ROOT/bin/gh"
export PATH="$ROOT/bin:$PATH" GH_REPO=owner/repo

commit() { # file content message
    printf '%s\n' "$2" >"$1"
    git add "$1"
    git commit -qm "$3"
}

# A fresh repo: main with a.txt, b.txt, AGENTS.md; the PR branch edits pr.txt.
# Leaves the work clone on the PR branch with R checked out.
setup() {
    local d=$ROOT/case$((++n))
    FX=$d/fx
    mkdir -p "$FX"
    export FX
    git init -q --bare "$d/remote.git"
    git init -q -b main "$d/work"
    cd "$d/work"
    commit a.txt a1 base
    commit b.txt b1 base-b
    commit AGENTS.md rules base-rules
    mkdir -p tools
    cp "$TOOLS/catchup_check.sh" "$TOOLS/review_gate.sh" tools/
    git add tools; git commit -qm tools
    git remote add origin "$d/remote.git"
    git push -q origin main
    git checkout -qb pr
    commit pr.txt p1 pr-change
    R=$(git rev-parse HEAD)
    git checkout -q main
    export CATCHUP_REMOTE=$d/remote.git
}

# Publish N as the PR head and main as it is now.
publish() {
    N=$(git rev-parse pr)
    git push -q -f origin main "pr:refs/pull/1/head"
    jq -n --arg sha "$N" '{state: "open", base: {ref: "main", repo: {default_branch: "main"}}, head: {sha: $sha}}' >"$FX/pr.json"
}

# comments.json from "author_association|edited|body" lines.
comments() {
    local line
    for line in "$@"; do
        IFS='|' read -r assoc edited body <<<"$line"
        jq -n --arg a "$assoc" --arg b "$body" --arg e "$edited" \
            '{author_association: $a, created_at: "t0", updated_at: (if $e == "edited" then "t1" else "t0" end), body: $b}'
    done | jq -s . >"$FX/comments.json"
}

# Expect exit status and a line in the output.
expect() { # name status pattern [args...]
    local name=$1 want=$2 pat=$3 got out
    shift 3
    set +e
    out=$(bash "$CHECK" 1 "$@" 2>&1)
    got=$?
    set -e
    if [[ $got != "$want" ]] || ! grep -q -- "$pat" <<<"$out"; then
        echo "FAIL $name: exit $got (want $want), output:"
        while IFS= read -r l; do echo "    $l"; done <<<"$out"
        fails=$((fails + 1))
    else
        echo "ok   $name"
    fi
}

catchup() { git checkout -q pr && git merge -q --no-edit main >/dev/null && git checkout -q main; }

n=0
setup; commit b.txt b2 main-b; catchup; publish
comments "OWNER|no|Review: clean @$R"
expect "clean catch-up passes" 0 "clean catch-up"
expect "--post re-posts R's marker for N" 0 "posted: Review: clean @$N" --post
[[ $(cat "$FX/posted.log") == "Review: clean @$N" ]] || { echo "FAIL posted exactly one marker: $(cat "$FX/posted.log")"; fails=$((fails + 1)); }
LOGIN=someone expect "--post refuses another account" 2 "isn't logged in as owner" --post

setup; commit b.txt b2 main-b; catchup; publish
comments "OWNER|no|Review: clean @$R" "OWNER|no|Security review: clean @$R" "OWNER|no|Second review: clean @$R"
expect "security set is reused" 0 "Second review:" --post
(( $(wc -l <"$FX/posted.log") == 3 )) || { echo "FAIL posted three markers"; fails=$((fails + 1)); }

setup; commit AGENTS.md rules2 main-rules; catchup; publish
comments "OWNER|no|Review: clean @$R" "OWNER|no|Security review: clean @$R" "OWNER|no|Second review: clean @$R"
expect "security PR, security path from main" 1 "security paths: AGENTS.md"
comments "OWNER|no|Review: clean @$R"
expect "non-security PR, security path from main" 0 "clean catch-up"

setup; commit b.txt b2 main-b; catchup; publish
comments "CONTRIBUTOR|no|Review: clean @$R"
expect "a contributor's marker doesn't count" 1 "no owner Review marker"
comments "OWNER|edited|Review: clean @$R"
expect "an edited marker doesn't count" 1 "no owner Review marker"
comments "OWNER|no|Review: clean @$R (looks good)"
expect "a marker with other text doesn't count" 1 "no owner Review marker"
comments "OWNER|no|Review: clean @$N"
expect "a marker for N isn't one for R" 1 "no owner Review marker"

# main and the PR change the same file in different places: merges cleanly,
# but the combination was never reviewed.
setup
printf '1\n2\n3\n4\n5\n6\n7\n8\n' >a.txt; git commit -qam lines; git push -q origin main
git checkout -q pr; git merge -q --no-edit main; sed -i.bak 's/^1$/one/' a.txt; rm a.txt.bak; git commit -qam pr-a
R=$(git rev-parse HEAD); git checkout -q main
sed -i.bak 's/^8$/eight/' a.txt; rm a.txt.bak; git commit -qam main-a
catchup; publish; comments "OWNER|no|Review: clean @$R"
expect "same file on both sides" 1 "main changed files the PR changes too: a.txt"

# main renames a file the PR edits.
setup
git checkout -q pr; commit a.txt a-pr pr-a; R=$(git rev-parse HEAD); git checkout -q main
git mv b.txt c.txt; git commit -qm rename; git mv a.txt a2.txt; git commit -qm rename-a
git checkout -q pr; git merge -q --no-edit main >/dev/null 2>&1
git checkout -q main; publish; comments "OWNER|no|Review: clean @$R"
expect "rename of a PR file is not clean" 1 "main changed files the PR changes too"

# A hand edit folded into the merge.
setup; commit b.txt b2 main-b
git checkout -q pr; git merge -q --no-edit main; echo extra >>b.txt; git commit -q --amend -a --no-edit; git checkout -q main
publish; comments "OWNER|no|Review: clean @$R"
expect "hand-edited merge" 1 "differs from git's own merge"

# A hand-resolved conflict.
setup
git checkout -q pr; commit b.txt b-pr pr-b; R=$(git rev-parse HEAD); git checkout -q main
commit b.txt b-main main-b
git checkout -q pr; git merge -q --no-edit main >/dev/null 2>&1 || true; echo resolved >b.txt; git commit -qam resolve; git checkout -q main
publish; comments "OWNER|no|Review: clean @$R"
expect "conflicted merge" 1 "has conflicts"

# Merging a branch that isn't main.
setup
git checkout -qb side; commit b.txt backdoor side; git checkout -q pr; git merge -q --no-edit side; git checkout -q main
publish; comments "OWNER|no|Review: clean @$R"
expect "merge of a non-main branch" 1 "not on main"

# Merge made from main's side (parents reversed).
setup; commit b.txt b2 main-b
git checkout -q -b tmp main; git merge -q --no-edit pr; git branch -qf pr tmp; git checkout -q main; git branch -qD tmp
publish; comments "OWNER|no|Review: clean @$R"
expect "reversed parents" 1 "not on main"

# A plain commit on top, no merge.
setup; git checkout -q pr; commit pr.txt p2 more; git checkout -q main; publish
comments "OWNER|no|Review: clean @$R"
expect "not a merge" 1 "is not a merge of two commits"

# The PR is closed.
setup; commit b.txt b2 main-b; catchup; publish
jq '.state = "closed"' "$FX/pr.json" >"$FX/x" && mv "$FX/x" "$FX/pr.json"
comments "OWNER|no|Review: clean @$R"
expect "closed PR" 1 "is not open"

# A name git still quotes (a tab in it) can't be matched against the paths.
setup; mkdir -p .github; commit $'.github/a\tb.yml' x main-odd; catchup; publish
comments "OWNER|no|Review: clean @$R" "OWNER|no|Security review: clean @$R" "OWNER|no|Second review: clean @$R"
expect "unusual file name" 1 "quotes or control characters"

# A Security review marker without a Review one isn't enough.
setup; commit b.txt b2 main-b; catchup; publish
comments "OWNER|no|Security review: clean @$R"
expect "security marker alone" 1 "no owner Review marker"

# Three parents.
setup; commit b.txt b2 main-b; git checkout -qb side main~1; commit s.txt s side; git checkout -q pr
git merge -q --no-edit main side >/dev/null; git checkout -q main; publish
comments "OWNER|no|Review: clean @$R"
expect "octopus merge" 1 "is not a merge of two commits"

# A PR into another branch.
setup; commit b.txt b2 main-b; catchup; publish
jq '.base.ref = "release"' "$FX/pr.json" >"$FX/x" && mv "$FX/x" "$FX/pr.json"
comments "OWNER|no|Review: clean @$R"
expect "base isn't the default branch" 1 "doesn't target main"

# The API's head isn't what the fetch got.
setup; commit b.txt b2 main-b; catchup; publish
jq --arg s "$R" '.head.sha = $s' "$FX/pr.json" >"$FX/x" && mv "$FX/x" "$FX/pr.json"
comments "OWNER|no|Review: clean @$R"
expect "head moved" 1 "isn't the PR's head"

# Reviews given against another base.
setup; commit b.txt b2 main-b; catchup; publish
echo '[{"event": "base_ref_changed"}]' >"$FX/timeline.json"
comments "OWNER|no|Review: clean @$R"
expect "retargeted PR" 1 "base branch was changed"
rm "$FX/timeline.json"; echo '[{"event": "automatic_base_change_succeeded"}]' >"$FX/events.json"
expect "automatically retargeted PR" 1 "base branch was changed"
touch "$FX/fail-events"; rm "$FX/events.json"
expect "events call fails" 1 ""
rm "$FX/fail-events"
echo '[{"event": "base_ref_changed"}]' >"$FX/timeline.json"; touch "$FX/fail-timeline"
expect "timeline call fails" 1 ""
rm "$FX/fail-timeline" "$FX/timeline.json"

# A copy of the script that isn't main's.
setup; commit b.txt b2 main-b; echo '# changed' >>tools/catchup_check.sh; git commit -qam tool-change; catchup; publish
comments "OWNER|no|Review: clean @$R"
expect "script differs from main's" 2 "differs from main's"
setup; commit b.txt b2 main-b; echo '# changed' >>tools/review_gate.sh; git commit -qam gate-change; catchup; publish
comments "OWNER|no|Review: clean @$R"
expect "gate differs from main's" 2 "review_gate.sh differs"

# A merge base that moved: the PR branched from another PR's fix, undid it,
# and that PR then landed by a merge commit. Merging main is clean and the
# files don't overlap, yet the head would undo main's fix unreviewed.
setup
git checkout -qb fix main; commit a.txt fixed fix; git checkout -q pr; git merge -q --no-edit fix >/dev/null
commit a.txt a1 undo-fix; R=$(git rev-parse HEAD); git checkout -q main
git merge -q --no-ff --no-edit fix >/dev/null; catchup; publish
comments "OWNER|no|Review: clean @$R"
expect "moved merge base" 1 "don't share one merge base"

# Malformed timeline data stops the check instead of passing it.
setup; commit b.txt b2 main-b; catchup; publish
echo '[1]' >"$FX/timeline.json"
comments "OWNER|no|Review: clean @$R"
expect "unreadable timeline" 2 "can't read"

(( fails == 0 )) || { echo "$fails failed"; exit 1; }
echo "all passed"
