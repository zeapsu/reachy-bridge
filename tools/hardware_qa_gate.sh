#!/usr/bin/env bash
# Sets the `hardware-qa` commit status on a PR's head commit. main's ruleset
# requires it, so a PR that changes what runs on the robot can't merge until
# the owner has tested its head on the robot and labelled the PR `qa: passed`.
#
#   GH_REPO=owner/name PR=<number> [EVENT=<action>] [EVENT_SHA=<sha>] \
#     [LABEL=<name>] [SENDER=<login>] [BASE_CHANGED=<old base>] bash tools/hardware_qa_gate.sh
#
# The workflow (.github/workflows/hardware-qa-gate.yml) passes the event's
# action, its head sha, the label it is about, who sent it and, on a
# retarget, the old base. A dispatch passes none of them.
#
# - No robot or audio file changed: success, "No hardware QA needed".
# - Otherwise pending, naming the areas (robot, audio, or unlisted files when
#   the files listing can't show them all), until the PR carries `qa: passed`
#   for its current head:
#   - opened, reopened, a new push (synchronize) and a new base (edited with
#     BASE_CHANGED) remove `qa: passed`: any QA was of other code;
#   - the run for the owner adding `qa: passed` passes the head it was added
#     at, when that is still the head, the label is still on (read live) and
#     this gate had already posted its pending status naming that head before
#     the label went on (so a push seconds before the label can't pass code
#     the owner never saw named);
#   - any other run (a dispatch, the owner removing the label, a run that
#     replaced a dropped one in the concurrency queue) passes only a head
#     this gate already passed and that still carries the label. A label on
#     a head the gate never passed was put on an earlier head, so it goes.
#
# GitHub keeps one pending run per concurrency group and cancels older ones,
# so a synchronize run can be dropped; the decision above comes from live
# state plus this gate's own earlier statuses, not from the event alone.
# A commit status belongs to the commit: a head shared with another open PR
# shares this status (same code, same QA).
#
# What it proves: the owner's account added `qa: passed` while this commit
# was the head, after this gate asked for QA of it; that the owner tested
# this commit rests on them checking the sha in the pending status against
# the robot's checkout before labelling. It can't prove QA ran, and agents
# act with that same account, so agents never add the label (AGENTS.md; the
# repo's Claude Code hook .claude/hooks/qa-guard.sh blocks the usual gh
# commands for it). Anything that can post statuses can also post this one.
set -Eeuo pipefail

: "${GH_REPO:?set GH_REPO}" "${PR:?set PR}"
[[ $PR =~ ^[1-9][0-9]*$ ]] || { echo "::error::not a PR number: $PR"; exit 1; }

QA_LABEL='qa: passed'
# AGENTS.md ("Channels") reads these three lines from this file; keep each a
# single-quoted one-liner.
# Robot: the controller, its Rust dependencies and build script, the runtime
# wrapper (motion, power and torque, return-home and the one-actuator guards,
# the daemon and its features, platform adapters), the dashboard code that
# drives the robot (the panels, hooks, API calls and logic for it, App.tsx
# that wires them up and api.ts that maps their routes), the installer,
# units, CLI and udev rules, the shipped webrtcsink build, the Python
# dependencies the robot runs (both pyproject.toml files: the root one is the
# uv workspace the installer syncs) and the toolchain config the installer's
# uv, cargo and bun builds read (mise, rustup, cargo, uv, bun and npm files,
# at any depth, as they look for them upward). Not web/package.json or
# web/bun.lock: the dashboard's packages are checked by the browser tests
# against the sim, and gating them would hold Dependabot's weekly web
# updates for QA.
ROBOT='^(controller/src/.+\.rs|controller/Cargo\.(toml|lock)|controller/build\.rs|runtime/src/reachy_bridge_runtime/.+\.py|web/src/(App|api|hooks/use(Drive|Power|Control|Expressions|Apps|Voice)|api/(power|control|expressions|apps|media)|logic/(drive|power|moves|pose|control|controls|expressions)|components/(DrivePanel|ControlStrip|ControlsPanel|ExpressionsPanel|AppsPanel))\.tsx?|deploy/.+|tools/udev/.+|tools/build_webrtcsink_jetson\.sh|(runtime/)?pyproject\.toml|uv\.lock|(.+/)?(\.cargo/.+|rust-toolchain(\.toml)?|\.tool-versions|\.?mise(\.[^/]+)?\.toml|\.?mise/.+|\.config/mise(\.[^/]+)?\.toml|\.config/mise/.+|\.python-versions?|uv\.toml|\.?bunfig\.toml|\.npmrc))$'
# Audio: what CI can't run without a sound card (the gateway, tee, mic mixer
# and wobble, the host setup, the controller's audio routes and ALSA volume,
# their units and the CLI that tees and checks them) and dashboard files that
# set volume, play sound or show audio (the voice pane, AudioPanel's levels,
# ControlsPanel's volume and test sound, api/media.ts's routes for them).
# audio_policy.py is only request signing, so it counts as robot alone.
AUDIO='^(runtime/src/reachy_bridge_runtime/((audio_[a-z_]+|mic_mixer|upstream)\.py|features/wobble\.py|platforms/[a-z_]+\.py)|controller/src/(audio|host)\.rs|web/src/(hooks/useVoice|components/(VoicePanel|AudioPanel|ControlsPanel)|logic/voice|api/(voice|media))\.tsx?|deploy/systemd/reachy-bridge-(audio|mic)\.service|deploy/reachy-bridge)$'
SIMONLY='^(runtime/src/reachy_bridge_runtime/features/sim_[a-z_]+\.py|deploy/systemd/reachy-bridge-sim\.service)$'

pr=$(gh api "repos/$GH_REPO/pulls/$PR")
if [[ $(jq -r .state <<<"$pr") != open ]]; then
    echo "PR #$PR is not open; nothing to do."
    exit 0
fi
sha=$(jq -r .head.sha <<<"$pr")
[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "::error::no head commit for #$PR"; exit 1; }
default=$(jq -r .base.repo.default_branch <<<"$pr")
base=$(jq -r .base.ref <<<"$pr")
if [[ $base != "$default" ]]; then
    echo "PR #$PR targets $base, not $default; its head's status is left to PRs into $default."
    exit 0
fi
url=$(jq -r .html_url <<<"$pr")
[[ -n ${GITHUB_RUN_ID:-} ]] && url="${GITHUB_SERVER_URL:-https://github.com}/$GH_REPO/actions/runs/$GITHUB_RUN_ID"

status() {
    echo "hardware-qa: $1 at ${sha:0:7}: $2"
    local try
    for try in 1 2 3; do
        gh api -X POST "repos/$GH_REPO/statuses/$sha" -f state="$1" -f context=hardware-qa \
            -f description="${2:0:140}" -f target_url="$url" >/dev/null && return 0
        (( try < 3 )) && sleep "${HARDWARE_QA_GATE_RETRY_DELAY:-5}"
    done
    return 1
}
# A run that fails from here on replaces any earlier success on this head.
trap 'status error "Hardware QA gate failed; re-run the Hardware QA gate workflow" || true' ERR

# Live, not the event's copy.
labelled=$(jq --arg l "$QA_LABEL" '[.labels[].name] | index($l) != null' <<<"$pr")
unlabel() {
    echo "Removing '$QA_LABEL': $1"
    if ! gh api -X DELETE "repos/$GH_REPO/issues/$PR/labels/$(jq -rn --arg l "$QA_LABEL" '$l|@uri')" >/dev/null; then
        # Fine if someone removed it meanwhile (a 404); otherwise the run fails.
        local left
        left=$(gh api --paginate "repos/$GH_REPO/issues/$PR/labels" | jq -r '.[].name')
        if grep -qxF "$QA_LABEL" <<<"$left"; then return 1; fi
    fi
    labelled=false
}

event=${EVENT:-}
case $event in
    opened | reopened | synchronize) fresh="a new head" ;;
    edited) fresh=${BASE_CHANGED:+a new base} ;;
    *) fresh= ;;
esac
# Any QA was of other code, whatever this PR changes.
if [[ -n $fresh && $labelled == true ]]; then unlabel "$fresh"; fi

files=$(gh api --paginate "repos/$GH_REPO/pulls/$PR/files" | jq -s 'add // []')
# Both names of a renamed file; sim-only files don't count.
names=$(jq -r '.[] | .filename, (.previous_filename // empty)' <<<"$files" |
    { grep -vE "$SIMONLY" || [[ $? == 1 ]]; })
areas=()
robot=$(grep -E "$ROBOT" <<<"$names" || [[ $? == 1 ]])
audio=$( { grep -vxF runtime/src/reachy_bridge_runtime/audio_policy.py <<<"$names" || [[ $? == 1 ]]; } |
    { grep -E "$AUDIO" || [[ $? == 1 ]]; })
[[ -n $robot ]] && areas+=(robot)
[[ -n $audio ]] && areas+=(audio)
# File names are the PR author's: no control characters reach the log.
while read -r h; do [[ -n $h ]] && echo "robot: ${h//[[:cntrl:]]/}"; done <<<"$robot"
while read -r h; do [[ -n $h ]] && echo "audio: ${h//[[:cntrl:]]/}"; done <<<"$audio"
# The files listing stops at 3000; what it can't show may be robot code.
if (( $(jq '.changed_files // 0' <<<"$pr") > $(jq length <<<"$files") )); then
    areas+=("unlisted files")
fi

if (( ${#areas[@]} == 0 )); then
    status success "No hardware QA needed"
    exit 0
fi
what=$(printf '%s, ' "${areas[@]}"); what=${what%, }

# So the owner has the label to add; a failure here isn't the gate's.
gh api -X POST "repos/$GH_REPO/labels" -f name="$QA_LABEL" -f color=0E8A16 \
    -f description="The owner tested this PR's head on the robot; a new push removes it" >/dev/null 2>&1 || true

passed=
if [[ $labelled == true ]]; then
    owner=${GH_REPO%%/*}
    # This gate's own statuses on this head (into a variable, so a failed
    # call fails the run).
    history=$(gh api --paginate "repos/$GH_REPO/commits/$sha/statuses" | jq -s 'add // []')
    if [[ $event == labeled && ${LABEL:-} == "$QA_LABEL" && ${EVENT_SHA:-} == "$sha" ]]; then
        if [[ ${SENDER:-} != "$owner" ]]; then
            unlabel "added by ${SENDER:-someone} rather than the owner"
        else
            # When the owner last added the label, and whether this gate had
            # asked for QA of this head before then. A push seconds before
            # the label makes a head the owner never saw named; so does a
            # PR whose gate never ran (the owner re-labels once it has).
            events=$(gh api --paginate "repos/$GH_REPO/issues/$PR/events" | jq -s 'add // []')
            at=$(jq -r --arg l "$QA_LABEL" --arg o "$owner" '[.[] | select(.event == "labeled"
                and .label.name == $l and .actor.login == $o) | .created_at | strings] | max // ""' <<<"$events")
            asked=$(jq --arg at "$at" --arg s "at ${sha:0:7};" '[.[] | select(.context == "hardware-qa"
                and .state == "pending" and .creator.login == "github-actions[bot]"
                and ((.description // "") | startswith("Needs hardware QA") and contains($s))
                and (.created_at | strings) < $at)] | length' <<<"$history")
            if [[ -n $at ]] && (( asked > 0 )); then
                passed="the owner added '$QA_LABEL' at this head after the gate asked for QA of it"
            else
                unlabel "added before this gate asked for QA of ${sha:0:7}; add it again after testing that commit"
            fi
        fi
    else
        # This gate's own success on this head, from an earlier run.
        prior=$(jq --arg c hardware-qa '
            [.[] | select(.context == $c and .state == "success"
                and .creator.login == "github-actions[bot]"
                and ((.description // "") | startswith("Hardware QA passed")))] | length' <<<"$history")
        if (( prior > 0 )); then
            passed="this head already passed and still has '$QA_LABEL'"
        else
            unlabel "it was added at an earlier head"
        fi
    fi
fi

if [[ -n $passed ]]; then
    echo "$passed"
    status success "Hardware QA passed ($what) at ${sha:0:7}"
else
    # The sha, so the owner labels only the head they deployed: a catch-up
    # with main (the Dependabot branch updater's, say) can land mid-QA.
    status pending "Needs hardware QA ($what) at ${sha:0:7}; label $QA_LABEL only after testing that commit on the robot"
fi
