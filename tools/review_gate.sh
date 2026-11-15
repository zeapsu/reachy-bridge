#!/usr/bin/env bash
# Sets the `review` commit status on a PR's head commit (#302): success once
# the repo owner has commented a clean review marker naming that commit,
# pending until then. main's ruleset requires the status, so a PR can't merge
# without the review record, wherever the merge comes from (a session with or
# without the local merge hook, the web UI, the API).
#
#   GH_REPO=owner/name PR=<number> bash tools/review_gate.sh
#
# Markers are lines in PR comments, as CONTRIBUTING.md ("Claiming work")
# describes, with 7 to 40 hex digits of the head commit:
#   Review: clean @<sha>                 every PR
#   Security review: clean @<sha>        security PRs: the `security` or
#   Second review: clean @<sha>          `auth` label, or a SECURITY_PATHS file
# Only comments by the repo owner count (author_association OWNER); anyone
# can comment on a public repo. Dependabot PRs whose every commit is
# Dependabot's, or a merge of main by the branch updater, pass without
# markers, as their auto-merge workflow already judges them.
#
# What it proves: a marker naming the head exists. It can't prove a review
# ran; that stays with the agent that posts it.
set -euo pipefail

: "${GH_REPO:?set GH_REPO}" "${PR:?set PR}"
[[ $PR =~ ^[0-9]+$ ]] || { echo "::error::not a PR number: $PR"; exit 1; }

# Login, sessions, pairing, the app proxy, bind addresses, request signing,
# app binding, CI and release workflows (update signing), and this gate.
SECURITY_PATHS='^(controller/src/(auth|profiles|setup|app_proxy|main)\.rs|runtime/src/reachy_bridge_runtime/(audio_policy|features/app_bind(_shim)?)\.py|deploy/(reachy-bridge|systemd/[^/]+\.service)|\.github/workflows/[^/]+|tools/review_gate\.sh)$'

# Every page of a list endpoint, as one JSON array.
list() { gh api --paginate "$1" | jq -s 'add // []'; }

pr=$(gh api "repos/$GH_REPO/pulls/$PR")
if [[ $(jq -r .state <<<"$pr") != open ]]; then
    echo "PR #$PR is not open; nothing to do."
    exit 0
fi
sha=$(jq -r .head.sha <<<"$pr")
[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "::error::no head commit for #$PR"; exit 1; }
url=$(jq -r .html_url <<<"$pr")

status() {
    echo "review: $1 at ${sha:0:7}: $2"
    gh api -X POST "repos/$GH_REPO/statuses/$sha" -f state="$1" -f context=review \
        -f description="${2:0:140}" -f target_url="$url" >/dev/null
}

if [[ $(jq -r .user.login <<<"$pr") == "dependabot[bot]" ]]; then
    # As in dependabot-auto-merge.yml: made on GitHub (committer web-flow,
    # signed by GitHub), by Dependabot or as the updater's merge of main.
    others=$(list "repos/$GH_REPO/pulls/$PR/commits" | jq -r '.[]
        | select((.committer.login == "web-flow" and .commit.verification.verified == true
            and (.author.login == "dependabot[bot]"
                 or (.author.login == "github-actions[bot]" and (.parents | length) > 1))) | not)
        | .sha')
    if [[ -z $others ]]; then
        status success "Dependabot update; no review marker needed"
        exit 0
    fi
    echo "Commits from someone other than Dependabot: $others"
fi

needed=(Review)
security=
if jq -e '.labels[].name | ascii_downcase | select(. == "security" or . == "auth")' <<<"$pr" >/dev/null; then
    security="label"
fi
# Both names of a renamed file.
hits=$(list "repos/$GH_REPO/pulls/$PR/files" | jq -r '.[] | .filename, (.previous_filename // empty)' |
    grep -E "$SECURITY_PATHS" || true)
if [[ -n $hits ]]; then
    security=${security:+$security, }"paths"
    while read -r h; do echo "security path: $h"; done <<<"$hits"
fi
[[ -n $security ]] && needed+=("Security review" "Second review")

# Marker lines from the owner's comments; tr drops CRLF line ends.
markers=$(list "repos/$GH_REPO/issues/$PR/comments" |
    jq -r '.[] | select(.author_association == "OWNER") | .body' | tr -d '\r')

missing=()
for name in "${needed[@]}"; do
    found=
    while read -r m; do
        [[ -n $m && $sha == "$m"* ]] && found=1
    done < <(grep -Eo "^$name: clean @[0-9a-f]{7,40}" <<<"$markers" | sed 's/.*@//' || true)
    [[ -n $found ]] || missing+=("$name")
done

join() { local s; s=$(printf '%s, ' "$@"); echo "${s%, }"; }
note=${security:+ (security PR: $security)}
if (( ${#missing[@]} == 0 )); then
    status success "$(join "${needed[@]}"): clean at ${sha:0:7}$note"
else
    status pending "Needs $(join "${missing[@]}") clean @${sha:0:7}$note"
fi
