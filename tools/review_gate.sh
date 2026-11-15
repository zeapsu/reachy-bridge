#!/usr/bin/env bash
# Sets the `review` commit status on a PR's head commit (#302): success once
# the repo owner's account has commented a clean review marker naming that
# commit, pending until then. main's ruleset requires the status, so a PR
# can't merge without the review record, wherever the merge comes from (a
# session with or without the local merge hook, the web UI, the API).
#
#   GH_REPO=owner/name PR=<number> bash tools/review_gate.sh
#
# Each marker is a PR comment of its own (not a review body) that is only the
# marker line, as CONTRIBUTING.md ("Claiming work") describes, with the full
# 40-digit sha of the head commit:
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
# A commit status belongs to the commit, not the PR, so only PRs into the
# default branch post one, and a head shared by more than one open PR into it
# stays pending.
#
# What it proves: a marker naming the head exists. It can't prove a review
# ran, and anything that can run a workflow with write access can post this
# status itself.
set -Eeuo pipefail

: "${GH_REPO:?set GH_REPO}" "${PR:?set PR}"
[[ $PR =~ ^[1-9][0-9]*$ ]] || { echo "::error::not a PR number: $PR"; exit 1; }

# Login, sessions, pairing and setup and the guards and headers that apply
# them, the app proxy, bind addresses and what's exposed, request signing,
# app binding and the sim's loopback redirect, app URLs and ports behind the
# proxy, secrets (the HF token and its scrubbing, sound tokens), the
# installer, dependency manifests and locks, build scripts, toolchains and
# the shipped webrtcsink build, CI and release workflows (update signing),
# agent rules and agent config, and this gate.
SECURITY_PATHS='^(controller/src/((auth|profiles|setup|app_proxy|main|lib|state|control|sim|audio|logs|apps)|routes/(store|sounds))\.rs|runtime/src/reachy_bridge_runtime/(main|audio_policy|audio_gateway|features/((app_bind|sim_apps)(_shim)?|sim_sounds))\.py|web/src/(api/(auth|hf|client|setup)|logic/(auth|setup))\.ts|web/src/components/(Login|Setup)\.tsx|deploy/(reachy-bridge|install\.sh|uninstall\.sh|systemd/[^/]+\.service)|(runtime/)?pyproject\.toml|uv\.lock|controller/Cargo\.(toml|lock)|web/(package\.json|bun\.lock)|controller/build\.rs|web/vite\.config\.ts|mise\.toml|tools/(toolchain_versions\.py|build_webrtcsink_jetson\.sh)|\.github/.+|\.claude/.+|\.mcp\.json|AGENTS\.md|CLAUDE\.md|CONTRIBUTING\.md|tools/(test_)?review_gate\.sh)$'

# Every page of a list endpoint, as one JSON array.
list() { gh api --paginate "$1" | jq -s 'add // []'; }

pr=$(gh api "repos/$GH_REPO/pulls/$PR")
if [[ $(jq -r .state <<<"$pr") != open ]]; then
    echo "PR #$PR is not open; nothing to do."
    exit 0
fi
sha=$(jq -r .head.sha <<<"$pr")
[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "::error::no head commit for #$PR"; exit 1; }
# The run's log says why it's pending; outside Actions, the PR.
url=$(jq -r .html_url <<<"$pr")
[[ -n ${GITHUB_RUN_ID:-} ]] && url="${GITHUB_SERVER_URL:-https://github.com}/$GH_REPO/actions/runs/$GITHUB_RUN_ID"
base=$(jq -r .base.ref <<<"$pr")
default=$(jq -r .base.repo.default_branch <<<"$pr")
if [[ $base != "$default" ]]; then
    echo "PR #$PR targets $base, not $default; its head's status is left to PRs into $default."
    exit 0
fi

status() {
    echo "review: $1 at ${sha:0:7}: $2"
    gh api -X POST "repos/$GH_REPO/statuses/$sha" -f state="$1" -f context=review \
        -f description="${2:0:140}" -f target_url="$url" >/dev/null
}
# A run that fails from here on replaces any earlier success on this head,
# so a lost marker or a new label can't leave a stale pass behind.
trap 'status error "Review gate failed; re-run the Review gate workflow" || true' ERR

# Another open PR into the default branch with this head would share this
# PR's status, whatever its own markers and labels say.
shared=$(list "repos/$GH_REPO/commits/$sha/pulls")
# The endpoint lists every PR that contains the commit; only heads share.
twins=$(jq -r --arg base "$default" --arg sha "$sha" --argjson n "$PR" \
    '.[] | select(.state == "open" and .base.ref == $base and .head.sha == $sha and .number != $n)
    | "#\(.number)"' <<<"$shared")
if [[ -n $twins ]]; then
    status pending "Head is also the head of $(tr '\n' ' ' <<<"$twins")- give each PR its own commit"
    exit 0
fi

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
names=$(jq -r '.[] | .filename, (.previous_filename // empty)' <<<"$files")
hits=$(grep -E "$SECURITY_PATHS" <<<"$names" || [[ $? == 1 ]])
if [[ -n $hits ]]; then
    security=${security:+$security, }"paths"
    while read -r h; do echo "security path: $h"; done <<<"$hits"
fi
# The files listing stops at 3000; treat what it can't show as security.
changed=$(jq '.changed_files // 0' <<<"$pr")
listed=$(jq length <<<"$files")
if (( changed > listed )); then
    security=${security:+$security, }"unlisted files"
fi
[[ -n $security ]] && needed+=("Security review" "Second review")

# The owner's unedited comments that are a marker for this head and nothing
# else, so a quoted template or a comment listing every marker counts for
# nothing. One per line in `markers`.
comments=$(list "repos/$GH_REPO/issues/$PR/comments")
markers=$(jq -r --arg sha "$sha" '.[]
    | select(.author_association == "OWNER" and .created_at == .updated_at)
    | .body | gsub("^\\s+|\\s+$"; "")
    | select(test("^(Review|Security review|Second review): clean @" + $sha + "$"))' <<<"$comments")

missing=()
for name in "${needed[@]}"; do
    grep -qxF "$name: clean @$sha" <<<"$markers" || missing+=("$name")
done
# Say why near misses don't count: other text, a short or other sha, an edit.
if (( ${#missing[@]} )); then
    jq -r --arg sha "$sha" '.[]
        | select(.author_association == "OWNER" and (.body | test("(Review|Security review|Second review): clean @")))
        | (.body | gsub("^\\s+|\\s+$"; "")) as $b
        | select(($b | test("^(Review|Security review|Second review): clean @" + $sha + "$") | not) or .created_at != .updated_at)
        | "not counted: comment \(.id) (\(if .created_at != .updated_at then "edited"
            elif ($b | test("@" + $sha + "([^0-9a-f]|$)") | not) then "not the full head sha"
            else "other text in the comment" end))"' <<<"$comments"
fi

join() { local s; s=$(printf '%s, ' "$@"); echo "${s%, }"; }
note=${security:+ (security PR: $security)}
if (( ${#missing[@]} == 0 )); then
    status success "$(join "${needed[@]}"): clean at ${sha:0:7}$note"
else
    status pending "Needs $(join "${missing[@]}") clean @<full head sha>$note"
fi
