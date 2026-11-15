#!/usr/bin/env bash
# Sets the `review` commit status on a PR's head commit (#302): success once
# the repo owner's account has commented a clean review marker naming that
# commit, pending until then. main's ruleset requires the status, so a PR
# can't merge without the review record, wherever the merge comes from (a
# session with or without the local merge hook, the web UI, the API).
#
#   GH_REPO=owner/name PR=<number> bash tools/review_gate.sh
#
# Markers are whole lines in PR comments (not review bodies), as
# CONTRIBUTING.md ("Claiming work") describes, with the full 40-digit sha of
# the head commit:
#   Review: clean @<sha>                 every PR
#   Security review: clean @<sha>        security PRs: the `security` or
#   Second review: clean @<sha>          `auth` label, or a SECURITY_PATHS file
# Only unedited comments by the repo owner's account count (author_association
# OWNER); anyone can comment on a public repo. That account is also the one
# agents act as, so OWNER means "posted with the owner's credentials".
# Dependabot PRs whose every commit is Dependabot's, or a merge of main by the
# branch updater, pass without markers, as their auto-merge workflow already
# judges them by their commits.
#
# What it proves: a marker naming the head exists. It can't prove a review
# ran, and anything that can run a workflow with write access can post this
# status itself.
set -euo pipefail

: "${GH_REPO:?set GH_REPO}" "${PR:?set PR}"
[[ $PR =~ ^[1-9][0-9]*$ ]] || { echo "::error::not a PR number: $PR"; exit 1; }

# Login, sessions and pairing and their guards, the app proxy, bind addresses
# and what's exposed, request signing, app binding, the installer, the
# HF token client, CI and release workflows (update signing), and this gate.
SECURITY_PATHS='^(controller/src/(auth|profiles|setup|app_proxy|main|lib|control|sim|audio)\.rs|runtime/src/reachy_bridge_runtime/(main|audio_policy|audio_gateway|features/app_bind(_shim)?)\.py|web/src/(api/auth|logic/auth|api/hf)\.ts|deploy/(reachy-bridge|install\.sh|uninstall\.sh|systemd/[^/]+\.service)|\.github/.+|tools/(test_)?review_gate\.sh)$'

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
# A run that fails from here on replaces any earlier success on this head,
# so a lost marker or a new label can't leave a stale pass behind.
trap 'status error "Review gate failed; re-run the Review gate workflow" || true' ERR

if [[ $(jq -r .user.login <<<"$pr") == "dependabot[bot]" ]]; then
    # As in dependabot-auto-merge.yml: made on GitHub (committer web-flow,
    # signed by GitHub), by Dependabot or as the updater's merge of main.
    commits=$(list "repos/$GH_REPO/pulls/$PR/commits")
    others=$(jq -r '.[]
        | select((.committer.login == "web-flow" and .commit.verification.verified == true
            and (.author.login == "dependabot[bot]"
                 or (.author.login == "github-actions[bot]" and (.parents | length) > 1))) | not)
        | .sha' <<<"$commits")
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
files=$(list "repos/$GH_REPO/pulls/$PR/files")
# Both names of a renamed file.
hits=$(jq -r '.[] | .filename, (.previous_filename // empty)' <<<"$files" | grep -E "$SECURITY_PATHS" || true)
if [[ -n $hits ]]; then
    security=${security:+$security, }"paths"
    while read -r h; do echo "security path: $h"; done <<<"$hits"
fi
# The files listing stops at 3000; treat what it can't show as security.
if (( $(jq '.changed_files // 0' <<<"$pr") > $(jq length <<<"$files") )); then
    security=${security:+$security, }"unlisted files"
fi
[[ -n $security ]] && needed+=("Security review" "Second review")

# Marker lines from the owner's unedited comments; tr drops CRLF line ends.
comments=$(list "repos/$GH_REPO/issues/$PR/comments")
markers=$(jq -r '.[] | select(.author_association == "OWNER" and .created_at == .updated_at) | .body' <<<"$comments" |
    tr -d '\r')

missing=()
for name in "${needed[@]}"; do
    grep -Eqx "$name: clean @${sha}[[:space:]]*" <<<"$markers" || missing+=("$name")
done

join() { local s; s=$(printf '%s, ' "$@"); echo "${s%, }"; }
note=${security:+ (security PR: $security)}
if (( ${#missing[@]} == 0 )); then
    status success "$(join "${needed[@]}"): clean at ${sha:0:7}$note"
else
    status pending "Needs $(join "${missing[@]}") clean @<full head sha>$note"
fi
