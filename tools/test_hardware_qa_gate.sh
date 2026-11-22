#!/usr/bin/env bash
# Tests tools/hardware_qa_gate.sh against a stub `gh` that answers from
# fixture files and logs the statuses and label changes it would make.
# Changes nothing outside a temp directory and makes no network calls.
#
#   bash tools/test_hardware_qa_gate.sh
set -euo pipefail

GATE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/hardware_qa_gate.sh
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
fails=0
HEAD=0123456789abcdef0123456789abcdef01234567
OLD=fedcba9876543210fedcba9876543210fedcba98

mkdir -p "$ROOT/bin"
# The stub serves $ROOT/fx/{pr,files,statuses,events}.json by endpoint, fails an
# endpoint when $ROOT/fx/fail-<name> exists (fail-statuses-<k>: only the next
# k status POSTs), logs a status POST as "<sha> <state> <description>" in
# status.log, and label changes as "delete <label>" / "create <label>" in
# labels.log.
cat >"$ROOT/bin/gh" <<'EOF'
#!/usr/bin/env bash
args=("$@")
method=GET
for ((i = 0; i < ${#args[@]}; i++)); do [[ ${args[i]} == -X ]] && method=${args[i + 1]}; done
for a in "${args[@]}"; do
    case $a in
        repos/*/commits/*/statuses) [[ -e $FX/fail-history ]] && exit 1; cat "$FX/statuses.json" 2>/dev/null || echo '[]'; exit 0 ;;
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
        repos/*/issues/*/events) [[ -e $FX/fail-events ]] && exit 1; cat "$FX/events.json" 2>/dev/null || echo '[]'; exit 0 ;;
        repos/*/issues/*/labels/*)
            [[ $method == DELETE ]] || break
            [[ -e $FX/fail-unlabel ]] && exit 1
            printf 'delete %s\n' "$(sed 's/%3A/:/g; s/%20/ /g' <<<"${a##*/}")" >>"$FX/labels.log"
            exit 0 ;;
        repos/*/issues/*/labels)
            [[ -e $FX/fail-labels ]] && exit 1
            [[ -e $FX/label-gone ]] && { echo '[]'; exit 0; }
            jq '.labels' "$FX/pr.json"; exit 0 ;;
        repos/*/labels)
            [[ -e $FX/fail-create ]] && exit 1
            for f in "${args[@]}"; do [[ $f == name=* ]] && echo "create ${f#name=}" >>"$FX/labels.log"; done
            exit 0 ;;
        repos/*/pulls/*/files) [[ -e $FX/fail-files ]] && exit 1; cat "$FX/files.json"; exit 0 ;;
        repos/*/pulls/*) cat "$FX/pr.json"; exit 0 ;;
    esac
done
echo "stub gh: unexpected call: $*" >&2
exit 1
EOF
chmod +x "$ROOT/bin/gh"
export PATH="$ROOT/bin:$PATH" FX="$ROOT/fx" GH_REPO=zeapsu/repo PR=7 HARDWARE_QA_GATE_RETRY_DELAY=0
unset GITHUB_OUTPUT GITHUB_RUN_ID GITHUB_ACTIONS

# setup [labels-json] FILE...: a fresh open PR into main changing FILEs.
setup() {
    rm -rf "$FX"
    mkdir -p "$FX"
    jq -n --arg sha "$HEAD" --argjson labels "${1:-[]}" \
        '{state: "open", head: {sha: $sha}, html_url: "https://example.invalid/pr/7",
          labels: [$labels[] | {name: .}], changed_files: 1,
          base: {ref: "main", repo: {default_branch: "main"}}}' >"$FX/pr.json"
    shift || true
    files "${@:-docs/manual.md}"
    unset EVENT EVENT_SHA LABEL SENDER BASE_CHANGED
}
files() {
    printf '%s\n' "$@" | jq -R '{filename: .}' | jq -s . >"$FX/files.json"
    jq --argjson n $# '.changed_files = $n' "$FX/pr.json" >"$FX/p" && mv "$FX/p" "$FX/pr.json"
}
# passed_before [CREATOR] [DESCRIPTION]: this gate passed HEAD on an earlier run.
passed_before() {
    jq -n --arg c "${1:-github-actions[bot]}" --arg d "${2:-Hardware QA passed (robot)}" \
        '[{context: "hardware-qa", state: "pending", description: "Checking for hardware QA", creator: {login: "github-actions[bot]"}},
          {context: "hardware-qa", state: "success", description: $d, creator: {login: $c}}]' >"$FX/statuses.json"
}
# asked [SHA] [AT] [LABELLED] [ACTOR]: this gate posted its pending status
# naming SHA (default HEAD) at AT, and ACTOR (default the owner) added
# qa: passed at LABELLED.
asked() {
    jq -n --arg s "${1:-$HEAD}" --arg at "${2:-2026-01-01T00:00:00Z}" \
        '[{context: "hardware-qa", state: "pending", description: "Checking for hardware QA", created_at: "2025-12-31T00:00:00Z", creator: {login: "github-actions[bot]"}},
          {context: "hardware-qa", state: "pending", created_at: $at, creator: {login: "github-actions[bot]"},
           description: ("Needs hardware QA (robot) at " + $s[0:7] + "; label qa: passed only after testing that commit on the robot")}]' >"$FX/statuses.json"
    jq -n --arg at "${3:-2026-01-01T00:05:00Z}" --arg who "${4:-zeapsu}" \
        '[{event: "labeled", label: {name: "qa: passed"}, actor: {login: "zeapsu"}, created_at: "2025-12-01T00:00:00Z"},
          {event: "labeled", label: {name: "other"}, actor: {login: "zeapsu"}, created_at: "2026-02-01T00:00:00Z"},
          {event: "labeled", label: {name: "qa: passed"}, actor: {login: $who}, created_at: $at}]' >"$FX/events.json"
}
# The owner adding qa: passed while HEAD is the head, after the gate asked
# for QA of it.
owner_labels() { export EVENT=labeled LABEL="qa: passed" SENDER=zeapsu EVENT_SHA=$HEAD; asked; }
fail() { echo "FAIL: $*"; fails=$((fails + 1)); }

# expect NAME STATE [TEXT]: the gate runs, sets STATE on HEAD, saying TEXT.
expect() {
    local name=$1 state=$2 text=${3:-}
    rm -f "$FX/status.log" "$FX/labels.log"
    if ! bash "$GATE" >"$FX/out" 2>&1; then fail "$name: gate failed"; cat "$FX/out"; return; fi
    local got
    got=$(cat "$FX/status.log" 2>/dev/null || true)
    if [[ $(grep -c . <<<"$got") != 1 ]]; then fail "$name: want one status, got '$got'"; cat "$FX/out"
    elif [[ $got != "$HEAD $state"* ]]; then fail "$name: want $state, got '$got'"; cat "$FX/out"
    elif [[ -n $text && $got != *"$text"* ]]; then fail "$name: '$got' doesn't say '$text'"
    else echo "ok: $name"; fi
}
# stripped NAME / kept NAME: whether the last run removed qa: passed.
stripped() {
    if grep -qxF "delete qa: passed" "$FX/labels.log" 2>/dev/null; then echo "ok: $1"
    else fail "$1: label not removed"; cat "$FX/out"; fi
}
kept() {
    if grep -q "^delete" "$FX/labels.log" 2>/dev/null; then fail "$1: label removed"; cat "$FX/out"
    else echo "ok: $1"; fi
}
# broken NAME: the gate exits non-zero and leaves an `error` status on HEAD.
broken() {
    rm -f "$FX/status.log" "$FX/labels.log"
    if bash "$GATE" >"$FX/out" 2>&1; then fail "$1: gate succeeded"; cat "$FX/out"; return; fi
    local got
    got=$(tail -n1 "$FX/status.log" 2>/dev/null || true)
    if [[ $got == "$HEAD error"* ]]; then echo "ok: $1"; else fail "$1: last status '$got'"; cat "$FX/out"; fi
}

# --- Which files need QA.
setup '[]' docs/manual.md README.md web/src/components/Profiles.tsx web/package.json web/bun.lock web/e2e/dashboard.spec.ts
expect "nothing robot-side: success" success "No hardware QA needed"
if [[ -e $FX/labels.log ]]; then fail "nothing robot-side: touched labels"; else echo "ok: nothing robot-side: no label created"; fi

for f in controller/src/main.rs controller/src/routes/power.rs controller/Cargo.toml controller/Cargo.lock \
    controller/build.rs runtime/src/reachy_bridge_runtime/main.py runtime/src/reachy_bridge_runtime/audio_policy.py \
    runtime/src/reachy_bridge_runtime/features/return_home.py web/src/hooks/useDrive.ts web/src/hooks/usePower.tsx \
    web/src/api/power.ts web/src/logic/moves.ts web/src/components/DrivePanel.tsx web/src/components/AppsPanel.tsx \
    deploy/install.sh deploy/systemd/reachy-bridge.service tools/udev/99-reachy-mini.rules \
    tools/build_webrtcsink_jetson.sh runtime/pyproject.toml uv.lock web/src/App.tsx web/src/api.ts \
    pyproject.toml mise.toml .mise.toml mise.local.toml controller/mise.toml .config/mise.toml \
    .config/mise/config.toml .mise/config.toml .cargo/config.toml controller/.cargo/config.toml \
    controller/.cargo/config rust-toolchain.toml controller/rust-toolchain .tool-versions .python-version \
    uv.toml web/bunfig.toml web/.npmrc .npmrc .cargo controller/.cargo .mise mise .config Cargo.toml Cargo.lock \
    package.json bun.lock bun.lockb; do
    setup '[]' docs/manual.md "$f"
    expect "robot path $f" pending "Needs hardware QA (robot) at 0123456;"
done

for f in web/src/components/VoicePanel.tsx web/src/logic/voice.ts web/src/api/voice.ts \
    web/src/components/AudioPanel.tsx; do
    setup '[]' "$f"
    expect "audio path $f" pending "Needs hardware QA (audio) at 0123456;"
done

for f in runtime/src/reachy_bridge_runtime/audio_gateway.py runtime/src/reachy_bridge_runtime/mic_mixer.py \
    runtime/src/reachy_bridge_runtime/upstream.py runtime/src/reachy_bridge_runtime/features/wobble.py \
    runtime/src/reachy_bridge_runtime/platforms/jetson.py controller/src/audio.rs controller/src/host.rs \
    web/src/hooks/useVoice.ts deploy/systemd/reachy-bridge-audio.service deploy/systemd/reachy-bridge-mic.service \
    deploy/reachy-bridge web/src/api/media.ts web/src/components/ControlsPanel.tsx; do
    setup '[]' "$f"
    expect "robot and audio path $f" pending "Needs hardware QA (robot, audio) at 0123456;"
done

setup '[]' runtime/src/reachy_bridge_runtime/audio_policy.py
expect "audio_policy.py alone is robot, not audio" pending "Needs hardware QA (robot) at 0123456;"

setup '[]' runtime/src/reachy_bridge_runtime/features/sim_apps.py deploy/systemd/reachy-bridge-sim.service tools/sim/run.py
expect "sim-only files don't count" success "No hardware QA needed"

setup '[]' web/src/api/powers.ts web/src/components/DrivePanelNotes.md controller/Cargo.toml.bak \
    runtime/src/reachy_bridge_runtime/notes.txt tools/udev controller/tests/power.rs web/src/hooks/useTheme.ts \
    web/src/AppView.tsx web/src/components/App.tsx web/src/api.test.ts web/src/api/mediax.ts docs/pyproject.toml.md \
    docs/mise.md docs/rust-toolchain.md .cargox .config/other.toml docs/package.json controller/package.json uv.toml.bak web/bunfig.json web/src/logic/audio.ts
expect "near-miss paths don't count" success "No hardware QA needed"

setup; jq -n '[{filename: "web/src/old.ts", previous_filename: "controller/src/power.rs"}]' >"$FX/files.json"
expect "renamed away from a robot path" pending "(robot)"

setup; jq '.changed_files = 3001' "$FX/pr.json" >"$FX/p" && mv "$FX/p" "$FX/pr.json"
expect "more files than the listing shows" pending "Needs hardware QA (unlisted files) at 0123456;"

setup '[]' controller/src/main.rs
expect "pending names the head's sha" pending "Needs hardware QA (robot) at 0123456;"
if grep -qxF "create qa: passed" "$FX/labels.log"; then echo "ok: qa: passed created"; else fail "label not created"; fi
touch "$FX/fail-create"
expect "a failed label create isn't the gate's failure" pending "Needs hardware QA (robot)"

setup '[]' controller/src/main.rs
expect "the description fits a status" pending
if (( $(cut -d' ' -f3- "$FX/status.log" | wc -c) <= 141 )); then echo "ok: description is 140 characters or fewer"
else fail "description too long: $(cat "$FX/status.log")"; fi

# --- The qa: passed label.
setup '["qa: passed"]' controller/src/main.rs; owner_labels
expect "owner adds qa: passed at the head" success "Hardware QA passed (robot) at 0123456"
kept "owner's label stays"

setup '["qa: passed"]' controller/src/audio.rs; owner_labels
expect "owner passes a robot and audio PR" success "Hardware QA passed (robot, audio)"

setup '["qa: passed"]' controller/src/main.rs; owner_labels; export SENDER=someone-else
expect "qa: passed from someone other than the owner" pending "Needs hardware QA"
stripped "someone else's label removed"

setup '["qa: passed"]' controller/src/main.rs; owner_labels; export EVENT_SHA=$OLD
expect "qa: passed added at an earlier head" pending "Needs hardware QA"
stripped "an earlier head's label removed"

setup '[]' controller/src/main.rs; owner_labels
expect "label event, but the label is gone again" pending
kept "nothing to remove"

setup '["qa: passed"]' controller/src/main.rs; owner_labels; export LABEL=other
expect "a labeled event for another label doesn't pass" pending
stripped "the label from an unknown head removed"

# The label counts only when this gate had asked for QA of this head first.
setup '["qa: passed"]' controller/src/main.rs; owner_labels; asked "$HEAD" 2026-01-01T00:00:00Z 2026-01-01T00:00:00Z
expect "qa: passed in the same second as the gate's pending" pending "Needs hardware QA"
stripped "a label no later than the pending is removed"
if grep -q "add it again after testing that commit" "$FX/out"; then echo "ok: the log says to add it again"
else fail "no re-label hint"; fi
setup '["qa: passed"]' controller/src/main.rs; owner_labels; asked "$HEAD" 2026-01-01T00:10:00Z
expect "qa: passed before the gate named this head" pending "Needs hardware QA"
stripped "a label from before the pending is removed"
setup '["qa: passed"]' controller/src/main.rs; owner_labels; asked "$OLD"
expect "the gate only ever named another head" pending "Needs hardware QA"
stripped "a pending for another head doesn't count"
setup '["qa: passed"]' controller/src/main.rs; owner_labels; echo '[]' >"$FX/statuses.json"
expect "the gate never asked for QA of this head" pending "Needs hardware QA"
stripped "no pending at all: label removed"
setup '["qa: passed"]' controller/src/main.rs; owner_labels; asked "$HEAD" 2026-01-01T00:00:00Z 2026-01-01T00:05:00Z someone-else
expect "only someone else's labeled event after the pending" pending "Needs hardware QA"
stripped "the owner's own label time decides"
setup '["qa: passed"]' controller/src/main.rs; owner_labels
jq '[.[] | .creator.login = "zeapsu"]' "$FX/statuses.json" >"$FX/s" && mv "$FX/s" "$FX/statuses.json"
expect "a pending the owner's account posted by hand doesn't count" pending
setup '["qa: passed"]' controller/src/main.rs; owner_labels
jq '.[1].description = null' "$FX/statuses.json" >"$FX/s" && mv "$FX/s" "$FX/statuses.json"
expect "a pending with no description doesn't count, and doesn't fail the run" pending
setup '["qa: passed"]' controller/src/main.rs; owner_labels; touch "$FX/fail-events"
broken "issue events listing fails: error"
setup '["qa: passed"]' controller/src/main.rs; owner_labels; echo '[1]' >"$FX/events.json"
broken "malformed issue events: error"

for ev in opened reopened synchronize; do
    setup '["qa: passed"]' controller/src/main.rs; passed_before; export EVENT=$ev EVENT_SHA=$HEAD
    expect "$ev: qa: passed goes" pending "Needs hardware QA (robot)"
    stripped "$ev removes qa: passed"
done

setup '["qa: passed"]' controller/src/main.rs; passed_before; export EVENT=edited BASE_CHANGED=feature
expect "new base: qa: passed goes" pending
stripped "new base removes qa: passed"

setup '["qa: passed"]' controller/src/main.rs; passed_before; export EVENT=edited
expect "an edit without a new base keeps a pass" success "Hardware QA passed"
kept "edit keeps the label"

setup '["qa: passed"]' docs/manual.md; export EVENT=synchronize
expect "new push on a PR with nothing robot-side" success "No hardware QA needed"
stripped "a new push removes qa: passed there too"

setup '[]' controller/src/main.rs; export EVENT=synchronize
expect "new push without the label" pending
kept "nothing to remove on a new push"

setup '["qa: passed"]' controller/src/main.rs; passed_before
expect "dispatch on a head this gate passed keeps the pass" success "Hardware QA passed (robot)"
kept "dispatch keeps the owner's label"

setup '["qa: passed"]' controller/src/main.rs
expect "dispatch on a head that never passed" pending
stripped "dispatch removes a label from an earlier head"

setup '["qa: passed"]' controller/src/main.rs; passed_before zeapsu
expect "a success the owner's account posted by hand doesn't count" pending
stripped "hand-posted success: label removed"

setup '["qa: passed"]' controller/src/main.rs; passed_before "github-actions[bot]" "No hardware QA needed"
expect "an earlier 'no QA needed' isn't a QA pass" pending

setup '["qa: passed"]' controller/src/main.rs; passed_before
jq '.[1].description = null' "$FX/statuses.json" >"$FX/s" && mv "$FX/s" "$FX/statuses.json"
expect "a success with no description isn't a pass, and doesn't fail the run" pending
stripped "no-description success: label removed"

setup '[]' controller/src/main.rs; passed_before; export EVENT=unlabeled LABEL="qa: passed" SENDER=zeapsu EVENT_SHA=$HEAD
expect "owner removes qa: passed: pending again" pending "Needs hardware QA"

# --- Other PRs and failures.
setup '[]' controller/src/main.rs
jq '.base.ref = "feature"' "$FX/pr.json" >"$FX/p" && mv "$FX/p" "$FX/pr.json"
rm -f "$FX/status.log"
if bash "$GATE" >"$FX/out" 2>&1 && [[ ! -e $FX/status.log ]]; then echo "ok: PR into another branch: no status"
else fail "PR into another branch: set a status or failed"; fi

setup '["qa: passed"]' controller/src/main.rs; export EVENT=synchronize
jq '.state = "closed"' "$FX/pr.json" >"$FX/p" && mv "$FX/p" "$FX/pr.json"
rm -f "$FX/status.log" "$FX/labels.log"
if bash "$GATE" >"$FX/out" 2>&1 && [[ ! -e $FX/status.log && ! -e $FX/labels.log ]]; then echo "ok: closed PR: nothing done"
else fail "closed PR: set a status, touched labels or failed"; fi

setup '[]' controller/src/main.rs; touch "$FX/fail-files"
broken "files listing fails: error"
setup '["qa: passed"]' controller/src/main.rs; touch "$FX/fail-history"
broken "status history fails: error"
setup '["qa: passed"]' controller/src/main.rs; export EVENT=synchronize; touch "$FX/fail-unlabel"
broken "removing the label fails: error"
setup '["qa: passed"]' controller/src/main.rs; export EVENT=synchronize; touch "$FX/fail-unlabel" "$FX/label-gone"
expect "label removed by someone else meanwhile" pending "Needs hardware QA (robot)"
setup '["qa: passed"]' controller/src/main.rs; export EVENT=synchronize; touch "$FX/fail-unlabel" "$FX/fail-labels"
broken "removing the label and re-reading the labels both fail: error"
setup '[]' controller/src/main.rs; touch "$FX/fail-statuses"
rm -f "$FX/status.log"
if bash "$GATE" >"$FX/out" 2>&1; then fail "status POST fails: gate succeeded"
else echo "ok: status POST fails: run fails"; fi
setup '[]' controller/src/main.rs; touch "$FX/fail-statuses-2"
expect "status POST fails twice, then lands" pending

setup '[]' $'controller/src/x\r\e[2K::warning::spoofed.rs'
expect "control characters in a file name" pending
if grep -q $'[\r\e]' "$FX/out"; then fail "control characters reached the log"; cat -v "$FX/out"
else echo "ok: no control characters in the log"; fi

for bad in "7; true" 07 0 ""; do
    if PR=$bad bash "$GATE" >"$FX/out" 2>&1; then fail "PR '$bad' accepted"
    else echo "ok: PR '$bad' refused"; fi
done

# The Claude Code hook that keeps agents from adding qa: passed or posting a
# hardware-qa status. guard WANT COMMAND: the hook exits WANT (2 blocks).
HOOK=$(dirname "$GATE")/../.claude/hooks/qa-guard.sh
guard() {
    local rc=0
    jq -n --arg c "$2" '{tool_name: "Bash", tool_input: {command: $c}}' | bash "$HOOK" >/dev/null 2>&1 || rc=$?
    if [[ $rc == "$1" ]]; then echo "ok: hook exits $1 for: $2"; else fail "hook exits $rc, want $1, for: $2"; fi
}
guard 2 'gh pr edit 5 --add-label "qa: passed"'
guard 2 "gh issue edit 5 --add-label 'QA: Passed'"
guard 2 'gh pr create --title x --body-file b --label "qa: passed"'
guard 2 'gh pr create -t x -F b -l "qa: passed"'
guard 2 'gh api -X POST repos/o/r/issues/5/labels -f "labels[]=qa: passed"'
guard 2 'gh api repos/o/r/issues/5/labels -f labels[]=qa:%20passed'
guard 2 'gh api --method PUT repos/o/r/issues/5/labels --input l.json # qa: passed'
guard 2 'gh api -X POST repos/o/r/statuses/abc -f state=success -f context=hardware-qa'
guard 2 'gh api repos/o/r/statuses/abc -f context=hardware-qa -f state=success'
guard 2 $'cd x &&\ngh pr edit 5 --add-label "qa: passed"'
guard 0 'gh pr edit 5 --remove-label "qa: passed"'
guard 0 'gh pr list --label "qa: passed"'
guard 0 'gh api repos/o/r/issues/5/labels | jq ".[] | select(.name == \"qa: passed\")"'
guard 0 'gh api --paginate repos/o/r/commits/abc/statuses | jq "select(.context == \"hardware-qa\")"'
guard 0 'gh workflow run hardware-qa-gate.yml --ref main -f pr=5'
guard 0 'gh pr checks 5 --watch'
guard 0 'gh pr create --title x --body-file body.md'
guard 0 'gh pr edit 5 --add-label security'
guard 0 'git log --grep "qa: passed" --add-label'
guard 2 'cd x && gh pr edit 5 --add-label "qa: passed"'
# shellcheck disable=SC2016  # the command's text, not expanded here
guard 2 'echo $(gh pr edit 5 --add-label "qa: passed")'
guard 2 'true; gh pr edit 5 --add-label "qa: passed"'
guard 0 $'git commit -F - <<EOF\nThe hook blocks gh commands that add qa: passed (--add-label).\nEOF'
guard 0 'echo "ugh pr edit 5 --add-label qa: passed"'
guard 2 'GH_REPO=o/r gh pr edit 5 --add-label "qa: passed"'
guard 2 'env GH_REPO=o/r gh pr edit 5 --add-label "qa: passed"'
# shellcheck disable=SC2016  # the command's text, not expanded here
guard 2 'for n in 5; do gh pr edit $n --add-label "qa: passed"; done'
guard 2 'if true; then gh pr edit 5 --add-label "qa: passed"; fi'
guard 2 '{ gh pr edit 5 --add-label "qa: passed"; }'
guard 0 'gh api -X GET repos/o/r/commits/abc/statuses -f per_page=100 --jq ".[] | select(.context == \"hardware-qa\")"'
# Without jq the hook reads the raw JSON, where gh follows a quote.
mkdir -p "$ROOT/nojq"
for t in cat grep; do ln -sf "$(command -v "$t")" "$ROOT/nojq/$t"; done
if printf '%s' '{"tool_input":{"command":"gh pr edit 5 --add-label \"qa: passed\""}}' |
    PATH=$ROOT/nojq /bin/bash "$HOOK" >/dev/null 2>&1; then fail "without jq, the hook lets a label through"
else echo "ok: without jq, the hook still blocks"; fi

# AGENTS.md ("Channels") reads ROBOT, AUDIO and SIMONLY from main's copy of
# this script with sed; each must come out whole and be a valid ERE.
gate=$(cat "$GATE")
pat() { sed -n "s/^$1='\(.*\)'\$/\1/p" <<<"$gate"; }
for v in ROBOT AUDIO SIMONLY; do
    p=$(pat "$v")
    rc=0; grep -E "$p" <<<"x" >/dev/null || rc=$?
    if [[ -z $p ]]; then fail "AGENTS.md can't read $v"
    elif (( rc > 1 )); then fail "$v isn't a valid ERE"
    else echo "ok: AGENTS.md reads $v"; fi
done
# shellcheck disable=SC2016  # the literal text of AGENTS.md's line
if grep -qF 'pat() { sed -n "s/^$1='"'"'\(.*\)'"'"'\$/\1/p" <<<"$gate"; }' "$(dirname "$GATE")/../AGENTS.md"; then
    echo "ok: AGENTS.md's pat() is the one tested here"
else fail "AGENTS.md's pat() differs from this test's"; fi
got=$(printf '%s\n' controller/src/main.rs web/src/components/AudioPanel.tsx deploy/systemd/reachy-bridge-sim.service docs/manual.md |
    grep -E "$(pat ROBOT)|$(pat AUDIO)" | grep -vE "$(pat SIMONLY)")
if [[ $got == $'controller/src/main.rs\nweb/src/components/AudioPanel.tsx' ]]; then echo "ok: the rc snippet picks robot and audio files"
else fail "rc snippet picked: $got"; fi

echo
if ((fails)); then echo "$fails failed"; exit 1; fi
echo "all passed"
