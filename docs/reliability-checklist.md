# Reliability checklist

One item per thing that has gone wrong, or nearly, in this repo. Each item has a trigger
readable from a diff, a check, and where it came from. Nobody runs the whole list:
`uv run python tools/checklist.py` reads the diff against `origin/main` and prints only the
items whose triggers match, and the reviewer checks those (see [`REVIEW.md`](../REVIEW.md)).
The script reads this file from `origin/main`, so a PR can't change the items it is judged
by.

## Format

```
### <id>: <title>
- **Paths:** `<regex>`   matched against each changed file's path
- **Diff:** `<regex>`    matched against each added or removed line
- **Check:** what the reviewer confirms (may continue on lines indented by two spaces)
- **From:** the issues or PRs behind it
```

An item matches when any changed path matches **Paths** or any changed line matches
**Diff**; either may be left out. Regexes are Python's, searched (not anchored unless they
say `^`). Changed lines in `REVIEW.md` and this file don't count, so editing the list
doesn't trigger every item.

Add an item when the same kind of miss shows up in two PRs, or one miss cost a hardware
QA round, and it can be spotted from a diff. Remove an item when a test or a check in CI
now catches it, or nothing has triggered it since the last two rc tags; the agent
proposes removals when it tags an rc (AGENTS.md, "Rules upkeep").

## Items

### daemon-power: Daemon start, stop and motor modes
- **Paths:** `^controller/src/(daemon|state)\.rs$|^controller/src/routes/power\.rs$`
- **Diff:** `daemon/(start|stop)|wake_up|goto_sleep|motor_mode|enable_motors`
- **Check:** `daemon/start` passes `wake_up` and `daemon/stop` passes `goto_sleep` explicitly,
  the controller assumes only what `docs/daemon-contract.md` says, and `tools/sim/smoke.py`
  covers the new path.
- **From:** #146 (four controller bugs from assumed daemon behaviour)

### actuating-guard: One actuating surface at a time
- **Paths:** `^controller/src/(control|expressions|apps)\.rs$|^controller/src/routes/(controls|drive|expressions|apps)\.rs$`
- **Diff:** `robot_app_lock|return_home|goto_target|take_over`
- **Check:** one app at a time, expressions serialized, apps and expressions mutually
  blocking, graceful return-home after motor re-enable, one driver under the control lock.
  No guard is weakened or bypassed by a new route; a test covers any change to them.
- **From:** #146 (robot slot), #245, #251, #254

### bind-port: Listening ports, binds and what's exposed
- **Diff:** `0\.0\.0\.0|\bbind\(|--host\b|SocketAddr|:8443\b|:7863\b|:804[0-9]\b|tailscale serve`
- **Check:** the daemon binds `127.0.0.1` only; anything on all interfaces sits behind the
  login; app UIs go through the logged-in proxy; nothing serves the signalling (:8443) or
  app ports through `tailscale serve` on their own port numbers.
- **From:** #158, #186, #213, #275

### telemetry: Upstream telemetry stays off
- **Paths:** `^deploy/systemd/|^tools/sim/(run|sim_daemon)\.py$`
- **Diff:** `REACHY_MINI_TELEMETRY|reachy_mini\.daemon|--fastapi-port`
- **Check:** every unit, script or entry point that starts a daemon sets
  `REACHY_MINI_TELEMETRY=0`.
- **From:** reachy-mini turns PostHog telemetry on by default (AGENTS.md, Architecture)

### subprocess-pipe: Subprocess output is drained
- **Diff:** `subprocess\.PIPE|Popen\(|create_subprocess_exec|Stdio::piped`
- **Check:** stdout and stderr are read continuously into bounded storage (a ring buffer, a
  log with a cap), so a chatty child can't block on a full pipe or grow memory forever.
- **From:** AGENTS.md hardware safety rules (an unread pipe stalls the app or daemon)

### media-capture: Camera and audio
- **Paths:** `(?i)(^|[/_-])(audio|camera|media|mic|voice)`
- **Diff:** `VideoCapture|/dev/video|/dev/snd|\barecord\b|\baplay\b|sounddevice|pyaudio|\balsa`
- **Check:** camera and audio come from the daemon's shared media, with no second capture of
  a device; the PR has the `audio: needs device test` label if it changes audio code, and
  the voice browser tests (`web/e2e/voice*.spec.ts`) cover new routes.
- **From:** #268, #273, #288

### app-lifecycle: Starting, stopping and updating apps
- **Paths:** `^controller/src/(apps|app_proxy)\.rs$|^runtime/src/reachy_bridge_runtime/features/(app_|apps_|sim_apps)`
- **Check:** camera and mic go back to the daemon after an app exits (also when it crashes);
  an app can't serve on one of the bridge's ports; app pins and per-profile app lists
  survive an update or reinstall.
- **From:** #273, #275, #290

### apps-venv: The apps venv
- **Paths:** `^uv\.lock$|^runtime/pyproject\.toml$|apps_venv`
- **Diff:** `apps_venv|opencv`
- **Check:** the apps venv stays on the daemon's `reachy-mini` and OpenCV versions; an app
  that needs a different OpenCV gets its own copy instead of changing the shared one.
- **From:** #279, #284

### upstream-bump: A new reachy-mini
- **Paths:** `^runtime/src/reachy_bridge_runtime/upstream\.py$`
- **Diff:** `reachy[-_]mini(\s*[>=<~]|-\d)`
- **Check:** `uv run reachy-bridge-runtime --check` passes, `docs/daemon-contract.md` matches
  the new version, the sim smoke test passes, and the PR waits for hardware QA before it
  merges (`docs/update-reachy-mini.md`).
- **From:** #155, #292 (1.14.0 broke the bridge)

### profiles: Profiles, pins and shared devices
- **Paths:** `^controller/src/profiles\.rs$|^controller/src/routes/store\.rs$|^web/src/.*([Pp]rofile|[Pp]in)`
- **Check:** a profile's switches (Power, Drive, Voice, Camera, Controls) are enforced by the
  controller, not only hidden in the dashboard; pins and stars survive an update and stay
  separate per device on a shared tablet.
- **From:** #290, #299, #301

### robot-off-gate: Deploy and restart refuse with the robot on
- **Paths:** `^deploy/(reachy-bridge|install\.sh|uninstall\.sh)$`
- **Diff:** `--force|robot-power|robot_power`
- **Check:** deploy, install and restart still refuse unless the robot is off (and `deploy`
  unless no app runs); nothing adds a way round it; `tools/test_cli.sh` covers the change.
- **From:** AGENTS.md robot host rules

### ui-copy: Dashboard text and states
- **Paths:** `^web/src/.*\.(tsx|css)$`
- **Check:** state shows through the UI rather than sentences, no justification text, the
  protective text stays (warnings before motion or going limp, security notes, the Take over
  line, pairing commands, real errors); `docs/manual.md` is updated; the browser baselines in
  `web/e2e/baselines/` only go down.
- **From:** the 2026-11-02 copy pass (direction B), #285, #286, #287

### private-values: No private values in tracked files
- **Diff:** `\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b|\.ts\.net\b|/Users/[A-Za-z]|\b192\.168\.\d+\.\d+\b`
- **Check:** no tailnet addresses or names, LAN addresses, SSH targets or home paths. Those
  go in a gitignored `CLAUDE.local.md` or the environment.
- **From:** AGENTS.md (this repo is public)

### review-gate: The checks that judge PRs
- **Paths:** `^(tools/(test_)?(review_gate|catchup_check)\.sh|tools/checklist\.py|\.github/workflows/review-gate\.yml|REVIEW\.md|docs/reliability-checklist\.md|\.claude/.+)$`
- **Check:** the change can't relax the checks that judge it (they run from `main`); every
  path that judges PRs is still in `SECURITY_PATHS`; the gate's tests cover the change.
- **From:** #302, #303, #309
