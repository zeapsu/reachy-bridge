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
# marker line (or the marker line and the Claude Code footer below it), as
# CONTRIBUTING.md ("Claiming work") describes, with the full 40-digit sha of
# the head commit:
#   Review: clean @<sha>                 every PR
#   Security review: clean @<sha>        security PRs: the `security` or
#   Second review: clean @<sha>          `auth` label, or a SECURITY_PATHS file
# Only unedited comments by the repo owner's account count (author_association
# OWNER), posted after the PR's base branch last changed (they reviewed
# another diff); anyone can comment on a public repo. That account is also the one
# agents act as, so OWNER means "posted with the owner's credentials".
# Dependabot PRs whose every commit is Dependabot's, or a merge of main by the
# branch updater, pass without markers when every update is a patch or minor
# one, nothing under .github/ changes and no security label is set, as their
# auto-merge workflow judges them; other Dependabot PRs (majors, unclassified
# updates, workflow bumps) need one Review marker.
#
# A commit status belongs to the commit, not the PR, so only PRs into the
# default branch post one, and a head shared by more than one open PR into it
# stays pending. When one of them closes, its run re-checks the others. That
# re-check runs in the closed PR's concurrency group, not the other PR's, so
# a marker deleted in the same few seconds can lose to it; re-run the
# workflow for that PR if its status looks stale.
#
# A failed status POST is retried; if GitHub keeps refusing it, the run
# fails, and the `error` it then tries to post may be refused too, leaving
# the head's earlier status in place (a commit status can only be replaced
# by another POST). Re-run the workflow once the API answers again.
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
# proxy, the host checks, the power, apps and logs routes, the upstream
# daemon shim and the apps venv sync, the udev rules (installed as root),
# secrets (the HF token and its scrubbing, sound tokens), the
# installer, dependency manifests and locks, build scripts, toolchains and
# the shipped webrtcsink build, CI and release workflows (update signing),
# agent rules and agent config for every coding agent (Claude, Codex and its
# .agents/ skills, Cursor, Gemini and the like, at any depth and any case, CLAUDE.local.md included even
# though it's gitignored), .gitmodules (what a checkout pulls in),
# .gitignore (what of it is tracked), .gitattributes (it can hide lines from
# diffs), the review focus and reliability checklist (reviewers read them
# from main), this gate, its catch-up check, the hardware QA gate and the
# local precheck hook.
# Directories match by their bare name as well, so a symlink called `.cursor`
# can't stand in for one; so do the parent directories of the fixed paths
# above (`Web` on macOS is web/, and a symlink there would swap in another
# web/package.json). A real directory never appears in a PR's file list, so
# only a file or symlink in its place matches. Besides this list, any file
# name with a character outside printable ASCII counts (see below).
SECURITY_PATHS='^(controller/src/((auth|profiles|setup|app_proxy|main|lib|state|control|sim|audio|logs|apps|host)|routes/(store|sounds|apps|power|logs))\.rs|runtime/src/reachy_bridge_runtime/(main|audio_policy|audio_gateway|upstream|features/((app_bind|sim_apps)(_shim)?|sim_sounds|apps_venv_sync))\.py|web/src/(api/(auth|hf|client|setup)|logic/(auth|setup))\.ts|web/src/components/(Login|Setup)\.tsx|deploy/(reachy-bridge|install\.sh|uninstall\.sh|systemd/[^/]+\.service)|(runtime/)?pyproject\.toml|uv\.lock|controller/Cargo\.(toml|lock)|web/(package\.json|bun\.lock)|controller/build\.rs|web/vite\.config\.ts|mise\.toml|tools/(toolchain_versions\.py|build_webrtcsink_jetson\.sh)|tools/udev/[^/]+\.rules|\.gitmodules|\.github(/.+)?|(.+/)?\.(claude|codex|agents|cursor|gemini|windsurf|clinerules|continue|roo|junie|kiro|opencode)(/.+)?|(.+/)?(\.(mcp\.json|cursorrules|windsurfrules|clinerules|aider\.conf\.yml)|opencode\.jsonc?)|\.gitignore|(.+/)?\.gitattributes|(.+/)?(AGENTS(\.override)?|CLAUDE(\.local)?|GEMINI)\.md|CONTRIBUTING\.md|REVIEW\.md|docs/reliability-checklist\.md|tools/checklist\.py|tools/(test_)?(review_gate|catchup_check|precheck|hardware_qa_gate)\.sh|controller(/src(/routes)?)?|runtime(/src(/reachy_bridge_runtime(/features)?)?)?|web(/src(/(api|logic|components))?)?|deploy(/systemd)?|tools(/udev)?|docs)$'

# Every page of a list endpoint, as one JSON array.
list() { gh api --paginate "$1" | jq -s 'add // []'; }
# Open PRs into the default branch, other than this one, whose head is $sha.
# (The endpoint lists every PR that contains the commit; only heads share.)
twins() {
    jq -r --arg base "$default" --arg sha "$sha" --argjson n "$PR" \
        '.[] | select(.state == "open" and .base.ref == $base and .head.sha == $sha and .number != $n)
        | .number' <<<"$1"
}

# An open PR into the default branch sharing this head was held pending
# while this PR was open into it too (a fork PR can reuse an owner PR's
# head). Closing or retargeting this one runs no gate for it, so check it
# here. Once: the re-check doesn't recurse.
recheck_twins() {
    [[ -z ${REVIEW_GATE_RECHECK:-} && $sha =~ ^[0-9a-f]{40}$ ]] || return 0
    # (Listings into variables, so a failed call fails the run instead of
    # reading as "no other PR".)
    local n prs open
    prs=$(list "repos/$GH_REPO/commits/$sha/pulls")
    open=$(twins "$prs")
    for n in $open; do
        echo "Re-checking #$n, which has the same head"
        # BASE_CHANGED is this PR's event, not the other PR's.
        PR=$n REVIEW_GATE_RECHECK=1 BASE_CHANGED='' bash "${BASH_SOURCE[0]}"
    done
}

pr=$(gh api "repos/$GH_REPO/pulls/$PR")
sha=$(jq -r .head.sha <<<"$pr")
default=$(jq -r .base.repo.default_branch <<<"$pr")
if [[ $(jq -r .state <<<"$pr") != open ]]; then
    echo "PR #$PR is not open; nothing to do for it."
    recheck_twins
    exit 0
fi
[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "::error::no head commit for #$PR"; exit 1; }
# The run's log says why it's pending; outside Actions, the PR.
url=$(jq -r .html_url <<<"$pr")
[[ -n ${GITHUB_RUN_ID:-} ]] && url="${GITHUB_SERVER_URL:-https://github.com}/$GH_REPO/actions/runs/$GITHUB_RUN_ID"
base=$(jq -r .base.ref <<<"$pr")
if [[ $base != "$default" ]]; then
    echo "PR #$PR targets $base, not $default; its head's status is left to PRs into $default."
    recheck_twins
    exit 0
fi

status() {
    echo "review: $1 at ${sha:0:7}: $2"
    local try
    for try in 1 2 3; do
        gh api -X POST "repos/$GH_REPO/statuses/$sha" -f state="$1" -f context=review \
            -f description="${2:0:140}" -f target_url="$url" >/dev/null && return 0
        (( try < 3 )) && sleep "${REVIEW_GATE_RETRY_DELAY:-5}"
    done
    return 1
}
# A run that fails from here on replaces any earlier success on this head,
# so a lost marker or a new label can't leave a stale pass behind.
trap 'status error "Review gate failed; re-run the Review gate workflow" || true' ERR

# Another open PR into the default branch with this head would share this
# PR's status, whatever its own markers and labels say.
prs=$(list "repos/$GH_REPO/commits/$sha/pulls")
shared=$(twins "$prs")
if [[ -n $shared ]]; then
    status pending "Head is also the head of #${shared//$'\n'/ #} - give each PR its own commit"
    exit 0
fi

files=$(list "repos/$GH_REPO/pulls/$PR/files")
# Both names of a renamed file.
names=$(jq -r '.[] | .filename, (.previous_filename // empty)' <<<"$files")

dependabot=
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
        # The update list Dependabot writes in its one commit's message, the
        # YAML between "updated-dependencies:" and "...": every entry needs
        # its own patch or minor update-type line, and nothing else counts.
        msgs=$(jq -r '[.[] | select(.author.login == "dependabot[bot]")]
            | if length == 1 then .[0].commit.message else "" end' <<<"$commits")
        small=$(awk '
            /^updated-dependencies:$/ { inblock = 1; next }
            inblock && /^\.\.\.$/ { inblock = 0 }
            !inblock { next }
            /^- dependency-name: / { n++; open = 1; next }
            /^  update-type: / {
                if (open && $0 ~ /^  update-type: version-update:semver-(patch|minor)$/) ok++
                else bad = 1
                open = 0
            }
            END { print (n > 0 && ok == n && !bad) ? "yes" : "no" }' <<<"$msgs")
        workflows=$(grep -E '^\.github/' <<<"$names" || [[ $? == 1 ]])
        labelled=$(jq -r '[.labels[].name | ascii_downcase | select(. == "security" or . == "auth")] | length' <<<"$pr")
        unlisted=$(( $(jq '.changed_files // 0' <<<"$pr") > $(jq length <<<"$files") ))
        if [[ $small == yes && -z $workflows && $labelled == 0 && $unlisted == 0 ]]; then
            status success "Dependabot patch or minor update; no review marker needed"
            exit 0
        fi
        dependabot=${workflows:+changes .github/}
        (( unlisted )) && dependabot="more files than the listing shows"
        [[ $small == yes ]] || dependabot="major or unclassified update"
        dependabot=${dependabot:-security label}
        echo "Dependabot $dependabot: needs a Review marker"
    else
        echo "Commits from someone other than Dependabot: $others"
    fi
fi

needed=(Review)
security=
if jq -e '.labels[].name | ascii_downcase | select(. == "security" or . == "auth")' <<<"$pr" >/dev/null; then
    security="label"
fi
# A Dependabot-only PR is judged by the label alone: its paths are always
# manifests, locks or workflows, and one review covers the update.
if [[ -z $dependabot ]]; then
    # Ignoring case: on a case-insensitive checkout (macOS) `.Claude/` or
    # `Claude.local.md` is the file the list names.
    hits=$(LC_ALL=C grep -iE "$SECURITY_PATHS" <<<"$names" || [[ $? == 1 ]])
    # And any name with a character outside printable ASCII: APFS folds
    # Unicode (a Kelvin sign is a K, so `uv.loc\u212A` is uv.lock there),
    # which no pattern keeps up with. Checked per name in jq, before a newline
    # in a name could split it into harmless-looking lines; logged JSON-quoted.
    odd=$(jq -r '.[] | .filename, (.previous_filename // empty) | select(test("[^ -~]")) | @json' <<<"$files")
    hits=$(printf '%s\n%s\n' "$hits" "$odd" | sed '/^$/d')
    if [[ -n $hits ]]; then
        security=${security:+$security, }"paths"
        # File names are the PR author's: no control characters (a CR could
        # rewrite the log line) reach the log.
        while read -r h; do echo "security path: ${h//[[:cntrl:]]/}"; done <<<"$hits"
    fi
    # The files listing stops at 3000; treat what it can't show as security.
    changed=$(jq '.changed_files // 0' <<<"$pr")
    listed=$(jq length <<<"$files")
    if (( changed > listed )); then
        security=${security:+$security, }"unlisted files"
    fi
fi
[[ -n $security ]] && needed+=("Security review" "Second review")

# The owner's unedited comments that are a marker for this head and nothing
# else, so a quoted template or a comment listing every marker counts for
# nothing. One per line in `markers`. The one exception is the footer Claude
# Code's cloud sessions must sign every GitHub comment with, exactly as below,
# which `body` drops before the check.
FOOTER='\n\n+---\n_Generated by \[Claude Code\]\(https://claude\.ai/code\)_$'
# shellcheck disable=SC2016  # jq's $footer, not the shell's
body='def body: .body | gsub("\r\n"; "\n") | gsub("^\\s+|\\s+$"; "") | sub($footer; "");'
comments=$(list "repos/$GH_REPO/issues/$PR/comments")
# When the PR's base last changed, as catchup_check.sh reads it: a manual
# change is in the timeline, an automatic one (a stacked PR whose base merged)
# in the issue events. Markers from before then reviewed another diff.
timeline=$(list "repos/$GH_REPO/issues/$PR/timeline")
events=$(list "repos/$GH_REPO/issues/$PR/events")
# (A change with no time fails the run rather than letting old markers count.)
since=$(jq -rs 'add | [.[] | select((.event // "") | strings | test("^(base_ref_changed|automatic_base_change_)"))]
    | if any(.[]; .created_at | type != "string") then error("a base change without a time")
      else map(.created_at) | max // "" end' <<<"$timeline$events")
# The run a retarget starts may read the timeline before the change shows up
# there. The workflow passes the event's old base as BASE_CHANGED; then the
# PR's last update, no earlier than the change, is the floor as well.
if [[ -n ${BASE_CHANGED:-} ]]; then
    updated=$(jq -r '.updated_at // "9999"' <<<"$pr")
    [[ $updated > $since ]] && since=$updated
fi
[[ -z $since ]] || echo "Base branch changed at $since; markers posted before it don't count."
markers=$(jq -r --arg sha "$sha" --arg footer "$FOOTER" --arg since "$since" "$body"'.[]
    | select(.author_association == "OWNER" and .created_at == .updated_at and .created_at > $since)
    | body
    | select(test("^(Review|Security review|Second review): clean @" + $sha + "$"))' <<<"$comments")

missing=()
for name in "${needed[@]}"; do
    grep -qxF "$name: clean @$sha" <<<"$markers" || missing+=("$name")
done
# Say why near misses don't count: other text, another commit, an edit, an
# older base.
if (( ${#missing[@]} )); then
    jq -r --arg sha "$sha" --arg footer "$FOOTER" --arg since "$since" "$body"'.[]
        | select(.author_association == "OWNER" and (.body | test("(Review|Security review|Second review): clean @")))
        | body as $b
        | select(($b | test("^(Review|Security review|Second review): clean @" + $sha + "$") | not)
            or .created_at != .updated_at or .created_at <= $since)
        | "not counted: comment \(.id) (\(if .created_at != .updated_at then "edited"
            elif .created_at <= $since then "posted before the base branch changed"
            elif ($b | test("@" + $sha + "([^0-9a-f]|$)") | not) then "another commit"
            else "other text in the comment" end))"' <<<"$comments"
fi

join() { local s; s=$(printf '%s, ' "$@"); echo "${s%, }"; }
note=${security:+ (security PR: $security)}${dependabot:+ (Dependabot: $dependabot)}
if (( ${#missing[@]} == 0 )); then
    status success "$(join "${needed[@]}"): clean at ${sha:0:7}$note"
else
    status pending "Needs $(join "${missing[@]}") clean @<full head sha>$note"
fi
