# Daemon behaviour contract

What the bridge can rely on from Pollen's daemon (`reachy-mini` 1.12.0), as
found in its source and confirmed on a Reachy Mini Lite. The controller is
built against this document, not against assumptions. When the pinned daemon
version changes, re-check every section against the new source.

Source paths below are relative to the installed `reachy_mini` package. The
bridge touches daemon internals only through
`runtime/src/reachy_bridge_runtime/upstream.py`, whose `REQUIREMENTS` list
every symbol it relies on; `reachy-bridge-runtime --check` verifies them.

## Process and ports

- The bridge runs the daemon as `reachy-bridge.service` with
  `--desktop-app-daemon --headless --no-autostart --no-wake-up-on-start`,
  bound to `127.0.0.1:8000`. The daemon process stays up; "power" is the
  backend inside it, started and stopped over HTTP.
- `--desktop-app-daemon` installs community apps into `apps_venv` next to the
  daemon venv, outside anything `uv sync` manages.
- WebRTC signalling runs inside webrtcsink on `:8443`
  (`run-signalling-server`). It needs gst-plugins-rs 0.14 on GStreamer 1.24;
  the 0.10 and 0.13 builds tried on the Jetson didn't have that property.
  Browsers don't need `:8443`: remote WebRTC goes through Pollen's central
  relay over HTTPS (`media.central_signaling_relay`). `:8443` serves the
  daemon's local relay and LAN desktop clients only, so the bridge doesn't
  publish it.
- Local camera sharing: the daemon publishes BGR frames at 10 fps through
  `unixfdsink` on `/tmp/reachymini_camera_socket`, and apps read them with
  `unixfdsrc` (`media/media_server.py`, `media/camera_gstreamer.py`). The
  socket exists only while the backend's media server runs.

## Daemon state and power

Endpoints: `daemon/app/routers/daemon.py`; implementation:
`daemon/daemon.py`.

| Call | Effect |
|---|---|
| `POST /api/daemon/start?wake_up=false` | Starts the backend without touching the motors. No motion. |
| `POST /api/daemon/start?wake_up=true` | Starts the backend, sets motor mode `enabled`, then runs `wake_up()`. |
| `POST /api/daemon/stop?goto_sleep=true` | Runs `reset_to_sleep()` (see below), then stops the backend. |
| `POST /api/daemon/stop?goto_sleep=false` | Stops the backend where it is. |

- On the real robot the reported motor mode is read from the motors
  (`_infer_control_mode` in `daemon/backend/robot/backend.py`): `disabled`
  when torque is off. After a cold start torque is off, so a start without
  wake-up reports `disabled` (limp). The MuJoCo backend always reports
  `enabled` and the mockup starts `enabled`, so in sim the same start shows
  as on. The sim tests patch the MuJoCo backend to start `disabled` and keep
  the mode it is set to (`tools/sim/sim_daemon.py`).
- Stop without the sleep move runs no Python code that disables torque; it
  closes the Rust motor controller. Whether torque stays on afterwards is not
  verified, and if it does, the next start reports `enabled`.
- `wake_up` and `goto_sleep` are required query parameters.
- Start and stop return `{"job_id": ...}` immediately and run as background
  jobs. A start/stop/restart sent while a job holds the lock returns `409
  Daemon is busy.` The check happens before the job takes the lock, so two
  requests sent close together can both be accepted.
- The `start` job waits up to 2 s for the backend to be ready, otherwise the
  state becomes `error`.
- `stop` skips the sleep move when the state is already `stopping` or `error`,
  whatever `goto_sleep` says.
- `GET /api/daemon/status` → `state` is one of `not_initialized`, `starting`,
  `running`, `stopping`, `stopped`, `error`. **`running` does not mean awake.**
  The motor mode is `backend_status.motor_control_mode` (`enabled`,
  `disabled`, `gravity_compensation`); `backend_status` is `null` when the
  backend is stopped.

The controller derives its robot state from these two fields
(`_robot_state` in `routes/daemon.py`):

| `state` | `motor_control_mode` | Controller reports |
|---|---|---|
| `running` | `enabled` or `gravity_compensation` | `running` (on) |
| `running` | `disabled` | `asleep` |
| anything else | — | the daemon state as is (`stopped`, `starting`, `stopping`, `error`, …) |

## Kinematics engine

- The backend's kinematics engine comes from `--kinematics-engine`
  (`AnalyticalKinematics` by default, or `Placo`). `daemon/start` reads it from
  `app.state.args` each time, so it can only change while the backend is down.
- Under gravity compensation the head and antenna motors run in current
  control and **ignore every move target**: gotos, expressions and an app's
  moves are accepted and do nothing, while audio carries on. The controller
  refuses it while an app runs and switches back to `enabled` before starting
  one (#150).
- Gravity compensation needs Placo. Under any other engine,
  `motors/set_mode/gravity_compensation` raises and the daemon answers a bare
  HTTP 500.
- `Daemon.restart` (REST `daemon/restart` and the WebRTC restart command)
  calls `start` without the engine, so upstream falls back to the default.
  The runtime's kinematics feature (`runtime/.../features/kinematics.py`)
  reuses the engine of the last explicit start.
- The daemon reports `stopped` (or `error`) slightly before it drops the
  backend, so "fully off" means `backend_status` is `null` too.
- Bridge route (`features/kinematics.py` in the runtime):
  - `GET /api/bridge/kinematics` returns `engine` (what the next power-on
    uses), `active` (the running backend's engine, `null` when off) and
    `available` (what imports).
  - `PUT` with `{"engine": ...}` switches the engine and saves it to
    `kinematics.json` in the bridge state dir. It returns 409 while the
    backend runs. The controller sequences power off → switch → power on
    (#150).
- Placo 0.9.14 needs `cmeel-urdfdom<5` and `cmeel-tinyxml2<11`. Newer builds
  resolve by default, and `import placo` then fails on a missing shared library.

## Moves

Backend: `daemon/backend/abstract.py`; endpoints:
`daemon/app/routers/move.py`, `daemon/app/routers/motors.py`.

- `wake_up()`, via `POST /api/move/play/wake_up`: goes to the init pose, pauses
  0.1 s, plays `wake_up.wav`, rolls the head 20° over 0.2 s and back over
  0.2 s. The roll is part of the move, not a glitch. It does nothing if the
  motors are already enabled and the commanded target is the init pose. **It
  does not enable the motors.** Waking a limp robot needs
  `POST /api/motors/set_mode/enabled` first.
- `goto_sleep()`, via `POST /api/move/play/goto_sleep`: moves to the init
  pose first if the head is far from it, plays `go_sleep.wav`, moves to the
  sleep pose over 2 s, waits, then sets the motors to `disabled`. It assumes
  position control; under gravity compensation or limp motors the trajectory
  is ignored and the head drops.
- `reset_to_sleep()` (used by `daemon/stop?goto_sleep=true` and the idle
  reset): turns the torque on, lifts to the init pose (0.3–1.5 s), then
  `goto_sleep()`. From a limp robot this looks like a wake followed by a
  sleep.
- **The dataset routes block the daemon's event loop.**
  - `GET /api/move/recorded-move-datasets/list/{dataset}` and `POST /api/move/play/recorded-move-dataset/...` are `async def` routes. They build `RecordedMoves(...)` synchronously: they resolve the Hugging Face snapshot and load every move file.
  - A cold list of the emotions library took about 5 s on the Jetson.
  - Moves run on that same loop, so a list fetched during the wake-up move froze the head mid-way, then the trajectory jumped ahead to catch up.
  - The controller caches the lists, keeping them on disk as well, and fetches them only while the robot is off (`controller/src/datasets.rs`).
- `POST /api/move/goto` and the play endpoints return a move UUID;
  `GET /api/move/running` lists the UUIDs still running, and
  `POST /api/move/stop` cancels one.
- Sounds: `Backend.play_sound(file)` (wake-up, sleep, a recorded move's
  sound, `volume/test-sound`, `media/play_sound`) hands the file to the media
  server: the path as given, else the name under the package's `assets/`.
  `media/play_sound` first tries a relative name in its upload folder
  (`/tmp/reachy_mini_sounds`). Move sounds are absolute paths into the
  dataset's Hugging Face cache; an uploaded move's audio is deleted when the
  move ends. A new sound stops the one playing. With `--no-media` there is no
  media server and nothing plays. **Bridge feature** (`features/sim_sounds.py`,
  sim daemons only, #219): every `play_sound` of a file in those folders
  (checked again when served), and every `stop_sound`, is recorded; `GET /api/bridge/sounds` lists the latest
  events and `GET /api/bridge/sounds/{token}` serves a played file by the
  random token its event names. The controller relays them to the dashboard in sim
  mode (`/api/sim/sounds-stream`, `/api/sim/sounds/{token}`).
- **REST moves are not serialized.** `play_move`'s guard (`_play_move_lock`)
  is a re-entrant `threading.RLock`, and every REST move runs on the same
  event-loop thread, so two `move/play/wake_up` calls both run and write
  targets at the same time. The controller serializes its own power and wake
  actions (#148).

## Robot slot (`robot_app_lock`)

Source: `daemon/robot_app_lock.py`. One managed owner at a time:

- `free`
- `local_app(name)`: a Python app started by the app manager
- `remote_session(name)`: a WebRTC client through the central relay

The current state is exposed at `GET /api/daemon/robot-app-lock-status`.

- A local app acquiring the slot evicts a remote session (the peer gets
  `endSession`). A remote session is refused while a local app holds it.
- SDK clients talking to the daemon directly (HTTP, WebSocket) bypass the
  lock entirely. The bridge's expressions and manual controls are such
  clients, so the controller must not send them while an app runs.

## Idle reset

Source: `request_idle_reset` and `_async_idle_reset` in
`daemon/backend/abstract.py`; wired in `Daemon.start`.

- **Trigger:** every transition of the slot to `free`. That covers an app
  exiting for any reason (clean exit, crash, kill, requested stop) and a
  remote session ending or dropping.
- **Grace:** 1.5 s (`IDLE_RESET_DEBOUNCE_S`), or 15 s
  (`IDLE_RESET_HANDOFF_GRACE_S`) when a remote client released the slot for a
  successor.
- **Cancelled by:** a local app start (`AppManager.start_app` calls
  `cancel_idle_reset`), a remote session acquiring the slot, or any WebRTC
  data-channel command (`_handle_webrtc_message`), during the grace period or
  mid-move.
- **Skipped when:** the motors are already `disabled` *and* the head is at the
  sleep pose, or the backend is not ready or is shutting down.
- **Action:** `reset_to_sleep()`, which ends with the motors disabled. Counting
  the grace, the lift, the move to the sleep pose and its final wait, the robot
  goes limp about 5–8 s after the slot frees.
  The daemon state stays `running`, so the bridge shows asleep.
- **Needs the backend's event loop**, which only exists with media enabled. A
  daemon started with `--no-media` never runs the idle reset. The sim tests
  (`tools/sim/sim_daemon.py`) give a `--no-media` backend that loop.
- **Bridge patch:** the runtime's keep-awake feature
  (`runtime/.../features/keep_awake.py`) detaches the trigger during
  `stop-current-app`. A requested stop then leaves the robot awake at the
  home pose; crashes and remote drops still put it to sleep.

## Apps

Source: `apps/manager.py`; endpoints: `daemon/app/routers/apps.py`.

- **One app at a time.** `POST /api/apps/start-app/{name}` returns `400 An
  app is already running` while the current app is `starting`, `running`,
  `stopping` or `error`.
- **Start does not wake the robot.** It takes the slot, cancels a pending
  idle reset and spawns the app process. An app started on a limp or asleep
  robot runs on a limp robot. The bridge wakes the robot first.
- `/start-app/{name}/no-evict` fails instead of evicting a remote session.
- **App state** (`GET /api/apps/current-app-status`, `null` if none):
  - `starting` until the monitor task runs, then `running`.
  - Exit code 0 → `done`. A `done` app does not block the next start.
  - Non-zero exit → `error`, with the last 10 stderr lines in `error`.
  - In every case the slot is released, which triggers the idle reset.
- **A crashed app blocks every start** until `POST
  /api/apps/stop-current-app` clears it. On an `error` app that call does not
  signal a process (it has already exited). It still runs the return-to-home
  step below, so it can move the robot. On a `done` or `stopping` app it
  returns `400 No app is currently running`.
- **Requested stop** (`stop-current-app`): SIGINT, then SIGKILL of the process
  tree after 20 s. Then:
  - Head within the sleep-pose radius: sets the motors to `disabled`.
  - Otherwise: enables the motors (real robot backend only) and moves to the
    init pose over 1 s.
  - That enable goes through `enable_motors()`, which leaves the motor mode
    as it was: after an app that ran limp, status reports `disabled` on a
    robot holding its pose. **Bridge patch:** the keep-awake wrapper sets the
    mode to `enabled` after the stop when torque is on and the mode says
    `disabled`.
- The app's UI address is `extra.custom_app_url` in the app metadata (for
  example `0.0.0.0:8042` for radio, `:7860` for conversation). The UI server
  comes up 1–3 s after the process starts, so the bridge polls it before
  sending the user there. The app binds that port itself, so nothing else
  may listen on it. Apps ask for every interface (`0.0.0.0` in
  `custom_app_url`, or Gradio's `server_name`). **Bridge patch (#186):**
  before each start the runtime writes a stdlib-only shim and a `.pth` into
  the app's venv, so every Python process there binds its TCP servers on
  loopback whatever address it asks for (a dual-stack `::` socket gets
  `::ffff:127.0.0.1`). An app that would run in the daemon's own venv, or
  whose venv can't take the shim, is refused, and a reachy-mini that no
  longer matches the hooks stops the runtime instead of running apps open.
  A specific address with port 0 is left alone (a client choosing its source
  address before `connect`). Not covered: a server that binds a specific LAN
  address or host name on port 0, listeners opened by native code (C
  extensions, non-Python child processes), Gradio `share` tunnels, and an app that undoes the shim
  on purpose; apps run as the bridge's user and are trusted at that level. The
  controller proxies the current app's UI from `:7863` (loopback, or every
  interface behind the login in LAN mode), published over HTTPS as :8043
  (#158).
- webrtcsink runs the signalling server (`run-signalling-server`) on
  `0.0.0.0:8443` upstream. **Bridge patch (#186):** `signalling-server-host`
  is set to `127.0.0.1` (a webrtcsink without the property fails the media
  server instead); the daemon's central relay connects to
  `ws://127.0.0.1:8443`. Only the Wireless model, out of scope here,
  advertises its LAN address for direct signalling. The app proxy never
  forwards to :8443.
- The dashboard shows an app's UI in a frame (#159), from a different origin
  (:8043 inside :10000, or :7863 inside :7861 over plain http). An app that sends
  `X-Frame-Options` or a CSP `frame-ancestors` that leaves out the dashboard
  shows a blank frame; ↗ opens it in its own tab. Checked 2026-10-28: the
  app base class in reachy-mini 1.12.0, Radio, Conversation App and Gradio's
  Python server send neither, and the proxy passes app headers through as
  they are. It strips the controller's own cookies (both ways), the local
  key header and hop-by-hop headers, and refuses cross-site writes and
  WebSocket upgrades.
- App processes get a scrubbed environment (`GST_*`, `PYTHONPATH`, `GI_*`,
  `XDG_*` removed) and import their own `reachy_mini` from `apps_venv`.

## Rules for the controller

These follow from the sections above. The first four are the bugs found in
#145's QA.

1. Show power from `state` *and* `motor_control_mode`. `running` alone is not
   "on".
2. Power on from off: `daemon/start?wake_up=true`. From asleep:
   `motors/set_mode/enabled`, then `move/play/wake_up`.
3. Before starting an app: clear an `error` app with `stop-current-app`,
   make sure the robot is awake, then start it.
4. Open an app's UI only after its `custom_app_url` answers.
5. Power off while asleep: `daemon/stop?goto_sleep=false`. The robot is
   already limp at the sleep pose, and `goto_sleep=true` would lift it first.
6. After any app exit other than a requested stop, expect the sleep move to
   start about 1.5 s later and the robot to be limp about 5–8 s after the exit,
   while the daemon stays `running`.
7. Don't send expressions, gotos or manual control while an app is running:
   the slot lock doesn't stop them.
