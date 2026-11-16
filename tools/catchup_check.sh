#!/usr/bin/env bash
# Checks whether a PR's newest commit is a clean catch-up with main that can
# reuse the review markers of the commit before it, and with --post re-posts
# those markers for the new head (AGENTS.md, gate 4).
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
#   (the PR's own changes), so every file in N is byte for byte the reviewed
#   PR's or main's;
# - R has an owner `Review: clean @R` marker, counted as the review gate
#   counts them (the owner's account, unedited, nothing else in the comment);
#   if R also has a `Security review` marker, nothing that came from main is
#   in the gate's SECURITY_PATHS.
# R is whatever N's first parent is; only its markers matter.
# --post then posts each of R's markers for N and nothing else, so it never
# adds a marker no reviewer gave; the review gate decides whether they are
# enough for N. Files that don't overlap can still interact; CI tests N.
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
g() { git -c diff.relative=false "$@"; }
[[ $(git rev-parse --is-shallow-repository) == false ]] \
    || { echo "shallow clone: run git fetch --unshallow first" >&2; exit 2; }

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

read -r -a parents <<<"$(git rev-list --parents -n1 "$N")"
(( ${#parents[@]} == 3 )) || fail "${N:0:7} is not a merge of two commits"
R=${parents[1]} M=${parents[2]}
# (A here-string, not a pipe: grep -q would end rev-list early under pipefail.)
grep -qx "$M" <<<"$(git rev-list --first-parent refs/catchup/main)" \
    || fail "${N:0:7} merged ${M:0:7}, which is not on $default (or the PR's commit isn't the first parent)"

rc=0
merged=$(git merge-tree --write-tree "$R" "$M") || rc=$?
(( rc != 1 )) || fail "merging ${R:0:7} and ${M:0:7} has conflicts"
(( rc == 0 )) || { echo "git merge-tree failed ($rc); it needs git 2.38 or newer" >&2; exit 2; }
[[ $merged == "$(git rev-parse "$N^{tree}")" ]] || fail "${N:0:7} differs from git's own merge (hand edits or another merge strategy)"

# Git quotes names with unusual bytes ("a\303\251"), which the path patterns
# can't match; such a name isn't a clean catch-up.
from_main=$(g -c core.quotePath=false diff --no-renames --name-only "$R" "$N")
own=$(g -c core.quotePath=false diff --no-renames --name-only "$M" "$N")
if grep -q '^"' <<<"$from_main"$'\n'"$own"; then fail "a changed file name has quotes or control characters"; fi
both=$(comm -12 <(sort <<<"$from_main") <(sort <<<"$own") | sed '/^$/d')
[[ -z $both ]] || fail "main changed files the PR changes too: $(tr '\n' ' ' <<<"$both")"

comments=$(gh api --paginate "repos/$repo/issues/$PR/comments" | jq -s 'add // []')
markers=$(jq -r --arg sha "$R" '.[]
    | select(.author_association == "OWNER" and .created_at == .updated_at)
    | .body | gsub("^\\s+|\\s+$"; "")
    | select(test("^(Review|Security review|Second review): clean @" + $sha + "$"))
    | sub(" clean @.*"; "")' <<<"$comments" | sort -u)
grep -qx 'Review:' <<<"$markers" || fail "${R:0:7} has no owner Review marker to reuse"
if grep -qx 'Security review:' <<<"$markers"; then
    hits=$(grep -E "$security_paths" <<<"$from_main" || [[ $? == 1 ]])
    [[ -z $hits ]] || fail "security PR, and main brought in security paths: $(tr '\n' ' ' <<<"$hits")"
fi

echo "clean catch-up: ${N:0:7} merges ${M:0:7} from $default into reviewed ${R:0:7}"
echo "markers on ${R:0:7}: $(tr '\n' ' ' <<<"$markers")"
[[ -n $POST ]] || { echo "(run again with --post to post them for ${N:0:7})"; exit 0; }

owner=${repo%%/*}
[[ $(gh api user -q .login) == "$owner" ]] || { echo "not posting: gh isn't logged in as $owner" >&2; exit 2; }
while read -r name; do
    gh api -X POST "repos/$repo/issues/$PR/comments" -f body="$name clean @$N" >/dev/null
    echo "posted: $name clean @$N"
done <<<"$markers"
