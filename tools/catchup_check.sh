#!/usr/bin/env bash
# Checks whether a PR's newest commit is a clean catch-up with main that can
# reuse the review markers of the commit before it, and with --post re-posts
# those markers for the new head (CONTRIBUTING.md, "Claiming work", step 6).
#
#   bash tools/catchup_check.sh <pr> [--post]
#
# It passes only when all of these hold, and exits 1 saying which failed:
# - the PR is open into the default branch, and its head N, fetched straight
#   from GitHub, is a merge with exactly two parents: R, the PR's previous
#   head, first, and M, a commit on main's first-parent history, second;
# - git merges R and M with no conflicts into exactly N's tree, so the merge
#   has no hand edits;
# - no file differs both between R and N (came from main) and between M and N
#   (the PR's own changes), names compared ignoring case (they collide on a
#   case-insensitive checkout), so every file in N is byte for byte the
#   reviewed PR's or main's;
# - R and M have one merge base, on main's first-parent history: the base
#   R's reviewer diffed against;
# - N's diff against M equals R's diff against that base (paths, modes and
#   blobs), so nothing in N's change is unreviewed;
# - the fetched main is GitHub's main;
# - the PR's base branch was never changed (timeline and issue events);
# - this script and review_gate.sh are main's own copies;
# - R has an owner `Review: clean @R` marker, counted as the review gate
#   counts them (the owner's account, unedited, nothing else in the comment
#   but the Claude Code footer);
#   if R also has a `Security review` marker, nothing that came from main is
#   in the gate's SECURITY_PATHS.
# R is whatever N's first parent is; only its markers matter.
# --post then posts each of R's markers for N and nothing else, so it never
# adds a marker no reviewer gave; the review gate decides whether they are
# enough for N. Files that don't overlap can still interact; CI tests N.
# A --post that fails partway says which markers it posted; the gate stays
# pending until the rest are there.
#
# It needs a full clone and git 2.38 or newer (merge-tree --write-tree), and
# relies on main taking only merge and squash merges: no fast-forwards and no
# merge queue, which could put a commit on main's first-parent history that
# the PR's reviewers never diffed against.
#
# Overrides for tests: GH_REPO (owner/name) and CATCHUP_REMOTE (a git URL).
set -euo pipefail

usage() { echo "usage: $0 <pr> [--post]" >&2; exit 2; }
[[ $# -ge 1 && $# -le 2 ]] || usage
PR=$1
POST=
[[ $# == 2 ]] && { [[ $2 == --post ]] || usage; POST=1; }
[[ $PR =~ ^[1-9][0-9]*$ ]] || usage

fail() { echo "not a clean catch-up: $*"; echo "Run the reviews again on the new head."; exit 1; }

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
security_paths=$(sed -n "s/^SECURITY_PATHS='\(.*\)'\$/\1/p" "$here/review_gate.sh")
[[ -n $security_paths ]] || { echo "can't read SECURITY_PATHS from review_gate.sh" >&2; exit 2; }

cd "$(git rev-parse --show-toplevel)"
# No local replace refs rewriting history.
export GIT_NO_REPLACE_OBJECTS=1
g() { git -c diff.relative=false "$@"; }
[[ $(git rev-parse --is-shallow-repository) == false ]] \
    || { echo "shallow clone: run git fetch --unshallow first" >&2; exit 2; }
if ! [[ $(git version) =~ ^git\ version\ ([0-9]+)\.([0-9]+) ]] \
    || (( BASH_REMATCH[1] < 2 || (BASH_REMATCH[1] == 2 && BASH_REMATCH[2] < 38) )); then
    echo "needs git 2.38 or newer (merge-tree --write-tree); this is $(git version)" >&2
    exit 2
fi

repo=${GH_REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}
pr=$(gh api "repos/$repo/pulls/$PR")
[[ $(jq -r .state <<<"$pr") == open ]] || fail "#$PR is not open"
default=$(jq -r .base.repo.default_branch <<<"$pr")
[[ $(jq -r .base.ref <<<"$pr") == "$default" ]] || fail "#$PR doesn't target $default"
N=$(jq -r .head.sha <<<"$pr")
[[ $N =~ ^[0-9a-f]{40}$ ]] || fail "no head commit for #$PR"

# Straight from the repo, not from whatever `origin` or a local main is.
remote=${CATCHUP_REMOTE:-https://github.com/$repo.git}
GIT_TERMINAL_PROMPT=0 git fetch -q --no-tags "$remote" "+refs/heads/$default:refs/catchup/main" "+refs/pull/$PR/head:refs/catchup/head"
[[ $(git rev-parse refs/catchup/head) == "$N" ]] || fail "the fetched head isn't the PR's head ${N:0:7}; try again"
api_main=$(gh api "repos/$repo/branches/$default" -q .commit.sha)
[[ $(git rev-parse refs/catchup/main) == "$api_main" ]] || fail "the fetched $default isn't GitHub's $default; try again"

# This script and the gate's path list must be main's: a PR's or a stale copy
# could post anything with the owner's credentials. Run it from current main.
for f in catchup_check.sh review_gate.sh; do
    [[ $(git hash-object "$here/$f") == "$(git rev-parse -q --verify "refs/catchup/main:tools/$f" || true)" ]] \
        || { echo "tools/$f differs from $default's; run this from a checkout of current $default" >&2; exit 2; }
done

# Markers given while the PR targeted another branch reviewed a different diff.
# GitHub lists a manual change in the timeline and an automatic one (a stacked
# PR whose base was merged) in the issue events, so read both. (Into
# variables, so a failed call stops the script instead of passing.)
timeline=$(gh api --paginate "repos/$repo/issues/$PR/timeline" | jq -s 'add // []')
events=$(gh api --paginate "repos/$repo/issues/$PR/events" | jq -s 'add // []')
rc=0
jq -e -s 'add | any(.[]; (.event // "") | strings | test("^(base_ref_changed|automatic_base_change_)"))' <<<"$timeline$events" >/dev/null || rc=$?
(( rc != 0 )) || fail "#$PR's base branch was changed; its earlier reviews may not cover what it now merges"
(( rc == 1 )) || { echo "can't read #$PR's timeline (jq exit $rc)" >&2; exit 2; }

# (Each listing into a variable, so a failed git call stops here with its
# own message instead of reading as an empty answer.)
line=$(git rev-list --parents -n1 "$N") || fail "can't read ${N:0:7}'s parents"
read -r -a parents <<<"$line"
(( ${#parents[@]} == 3 )) || fail "${N:0:7} is not a merge of two commits"
R=${parents[1]} M=${parents[2]}
spine=$(git rev-list --first-parent refs/catchup/main) || fail "can't list $default's first-parent history"
# (A here-string, not a pipe: grep -q would end the listing early under pipefail.)
grep -qx "$M" <<<"$spine" \
    || fail "${N:0:7} merged ${M:0:7}, which is not on $default (or the PR's commit isn't the first parent)"

# The reviewer of R saw its diff from where it left main's first-parent
# history; the merge must use that same base. A base that moved (main took in
# a commit from R's own history by a merge) or several bases would let N
# differ from main in ways no reviewer saw.
base=$(git merge-base --all "$R" "$M") || fail "${R:0:7} and ${M:0:7} have no merge base"
if ! [[ $base =~ ^[0-9a-f]{40}$ ]] || ! grep -qx "$base" <<<"$spine"; then
    fail "${R:0:7} and ${M:0:7} don't share one merge base on $default's first-parent history"
fi

rc=0
merged=$(git merge-tree --write-tree "$R" "$M") || rc=$?
(( rc != 1 )) || fail "merging ${R:0:7} and ${M:0:7} has conflicts"
(( rc == 0 )) || { echo "git merge-tree failed ($rc); it needs git 2.38 or newer" >&2; exit 2; }
[[ $merged == "$(git rev-parse "$N^{tree}")" ]] || fail "${N:0:7} differs from git's own merge (hand edits or another merge strategy)"

# Git quotes names with control characters, `"` or `\` ("a\tb"), which the
# path patterns can't match; such a name isn't a clean catch-up.
from_main=$(g -c core.quotePath=false diff --no-renames --name-only "$R" "$N")
own=$(g -c core.quotePath=false diff --no-renames --name-only "$M" "$N")
# Nor is a name outside printable ASCII: macOS folds Unicode case (a Kelvin
# sign is a K), so it can be a security path or collide with another name.
if LC_ALL=C grep -qE '^"|[^ -~]' <<<"$from_main"$'\n'"$own"; then
    fail "a changed file name has quotes, control characters or non-ASCII characters"
fi
# Ignoring case: README and readme are one file on a case-insensitive checkout.
lower() { tr '[:upper:]' '[:lower:]' <<<"$1" | LC_ALL=C sort -u; }
both=$(LC_ALL=C comm -12 <(lower "$from_main") <(lower "$own") | sed '/^$/d')
[[ -z $both ]] || fail "main changed files the PR changes too: $(tr '\n' ' ' <<<"$both")(names in lower case)"

# The invariant itself, whatever the merge machinery or local settings did:
# N's change against M is exactly R's reviewed change against its base (same
# paths, modes and blobs).
# (Into variables first: a failed diff then stops the script, where inside
# [[ ]] two empty outputs would compare equal.)
raw() { g diff --raw --no-abbrev --no-renames --ignore-submodules=none "$@"; }
reviewed=$(raw "$base" "$R")
now=$(raw "$M" "$N")
[[ $reviewed == "$now" ]] \
    || fail "${N:0:7}'s change against $default isn't exactly what ${R:0:7}'s reviewers saw"

comments=$(gh api --paginate "repos/$repo/issues/$PR/comments" | jq -s 'add // []')
# Counted as review_gate.sh counts them: a bare marker, or one followed only by
# the Claude Code footer.
FOOTER='\n\n+---\n_Generated by \[Claude Code\]\(https://claude\.ai/code\)_$'
markers=$(jq -r --arg sha "$R" --arg footer "$FOOTER" '.[]
    | select(.author_association == "OWNER" and .created_at == .updated_at)
    | .body | gsub("\r\n"; "\n") | gsub("^\\s+|\\s+$"; "") | sub($footer; "")
    | select(test("^(Review|Security review|Second review): clean @" + $sha + "$"))
    | sub(" clean @.*"; "")' <<<"$comments" | sort -u)
grep -qx 'Review:' <<<"$markers" || fail "${R:0:7} has no owner Review marker to reuse"
if grep -qx 'Security review:' <<<"$markers"; then
    hits=$(LC_ALL=C grep -iE "$security_paths" <<<"$from_main" || [[ $? == 1 ]])  # any case, as the gate
    [[ -z $hits ]] || fail "security PR, and main brought in security paths: $(tr '\n' ' ' <<<"$hits")"
fi

echo "clean catch-up: ${N:0:7} merges ${M:0:7} from $default into reviewed ${R:0:7}"
echo "markers on ${R:0:7}: $(tr '\n' ' ' <<<"$markers")"
[[ -n $POST ]] || { echo "(run again with --post to post them for ${N:0:7})"; exit 0; }

owner=${repo%%/*}
[[ $(gh api user -q .login) == "$owner" ]] || { echo "not posting: gh isn't logged in as $owner" >&2; exit 2; }
while read -r name; do
    if ! gh api -X POST "repos/$repo/issues/$PR/comments" -f body="$name clean @$N" >/dev/null; then
        echo "posting $name clean @$N failed; the markers above were posted, the rest weren't." >&2
        echo "The review gate stays pending until all are there: run --post again or post the rest by hand." >&2
        exit 2
    fi
    echo "posted: $name clean @$N"
done <<<"$markers"
