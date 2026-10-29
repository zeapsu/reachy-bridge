# Sim integration tests

Drives the controller against Pollen's daemon in MuJoCo sim mode, so the
controller <-> daemon bugs show up before a hardware QA pass. Only the
simulated robot moves. CI runs this as the `sim` job.

```bash
uv sync
(cd web && bun install --frozen-lockfile && bun run build)   # the controller embeds web/dist
(cd controller && cargo build --release --locked)
uv run python tools/sim/run.py
```

`run.py` starts its own sim daemon and controller on free ports, installs the
test apps into a fresh apps venv, waits for both servers, runs `smoke.py`
and stops everything. It doesn't touch a daemon or controller you already
run, or the checkout's `apps_venv`. A run takes about 40 s. Logs, state and
the apps venv go in a temporary directory that is kept (and printed) when a
check fails; `--work-dir DIR` puts them in `DIR` and always keeps them. Set
`SIM_CONTROLLER_BIN` to test another controller binary.

## What it checks

`smoke.py` holds the checks. Those tagged `[bug N]` cover the bugs from #146,
where the controller assumed daemon behaviour that isn't true (see
`docs/daemon-contract.md`):

| Bug | Checks |
|---|---|
| 1. `daemon/start?wake_up=false` leaves the motors limp | power on (from off, and from asleep) ends `running` with motors `enabled` |
| 2. The daemon sleeps the robot ~1.5 s after an app exits and stays `running` | after the crash app exits, the daemon reports `running`/`disabled` and the controller `asleep` |
| 3. Starting an app doesn't wake the robot | an app started while off, and while asleep, runs with motors `enabled` |
| 4. An app's web server isn't up when its process starts | the controller reports the UI not ready while the server is down, then ready, and the UI answers |

It also checks power off, expressions refused while an app runs, a requested
stop keeping the robot awake (the runtime's keep-awake feature), and a second
app start.

## How the sim is made to behave like the robot

`sim_daemon.py` runs `reachy-bridge-runtime` with two sim-only patches,
because stock MuJoCo sim hides bugs 1 and 2:

- The MuJoCo backend always reports motors `enabled`. The patch keeps the
  mode it is set to and starts `disabled`, like the real robot after a cold
  start.
- The daemon's idle reset runs on an event loop only the media server
  creates, so a `--no-media` daemon never sleeps the robot after an app
  exits. The patch gives the backend that loop; the idle reset itself is the
  daemon's own code.

`REACHY_BRIDGE_APPS_ROOT` points the daemon's `apps_venv` at the test run's directory (the runtime's sim_apps feature, #218, which also points the apps at the sim daemon's port).

## Test apps

`apps/` holds two local apps, so the tests need no Hugging Face Space:

- `sim_ui_app`: serves a page on `127.0.0.1:18742`, starting the server
  `SIM_UI_DELAY` seconds (default 3) after the process, as real app UIs come
  up late.
- `sim_crash_app`: no UI; exits with code 1 after a second.

`run.py` installs them with `uv pip install` into the apps venv rather than
through the daemon's install API, which would also install the latest
`reachy-mini` from PyPI into it.

## Against servers you started yourself

`smoke.py` alone runs against `SIM_DAEMON` and `SIM_CONTROLLER` (default
`http://127.0.0.1:8100` and `:7862`). Start the daemon through
`sim_daemon.py` (same arguments as `reachy-bridge-runtime`) with the test
apps installed in its apps venv. Against a stock sim daemon the bug 2 checks
fail, and the bug 1 and 3 checks pass whatever the controller does.
