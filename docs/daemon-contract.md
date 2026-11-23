# Daemon behaviour contract

What the bridge can rely on from Pollen's daemon (`reachy-mini` 1.14.0), as
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
  daemon venv, outside anything `uv sync` manages. Upstream installs
  `reachy-mini` there once, unpinned, and never moves it to match the daemon.
  **Bridge step (`features/apps_venv_sync.py`):** `install.sh` sets the
  `reachy-mini` and OpenCV packages an apps venv already has (the robot's and
  the sim's) to the daemon's versions. An app install can still move them
  until the next install.
- WebRTC signalling runs inside webrtcsink on `:8443`
  (`run-signalling-server`). It needs gst-plugins-rs 0.14 on GStreamer 1.24;
  the 0.10 and 0.13 builds tried on the Jetson didn't have that property.
  Browsers don't need `:8443`: remote WebRTC goes through Pollen's central
  relay over HTTPS (`media.central_signaling_relay`). `:8443` serves the
  daemon's local relay and LAN desktop clients only, so the bridge doesn't
  publish it.
- Telemetry (since 1.13.0, `daemon/telemetry.py`): on by default, the
  daemon posts usage events every 60 s to PostHog EU
  (`eu.i.posthog.com`): daemon start, a daily heartbeat, app start, stop,
  install and removal with the public Space id, update results and client
  connections, tagged with a random install id, versions, OS and arch.
  1.14.0 adds a `usage_tick` (hourly while in use, and when use stops: active
  seconds, client kinds, app ids), `js_app_started`/`js_app_stopped` from
  a WebRTC client's new `client_hello`, and `move_played` (Pollen's move
  names, other datasets as `other`); `GetVersion`'s `client_hello` is
  `true` only while telemetry is active. All of it goes through the same
  `capture`, a no-op while inactive. With `REACHY_MINI_TELEMETRY=0` (or
  `DO_NOT_TRACK=1`) the daemon starts without it and writes no telemetry
  files, and neither the WebRTC `set_telemetry` command nor
  `PUT /api/telemetry` can turn it back on. The bridge's units, the sim
  test runner and `tools/sim/sim_daemon.py` set it.
  1.14.0 adds no other outbound call (its `adopt_legacy_login`, a local
  file copy, runs only on the Wireless model).
- Local camera sharing: the daemon publishes BGR frames at 10 fps through
  `unixfdsink` on `/tmp/reachymini_camera_socket`, and apps read them with
  `unixfdsrc` (`media/media_server.py`, `media/camera_gstreamer.py`). The
  socket exists only while the backend's media server runs.
- Media release (`POST /api/media/release`, `.../acquire`): an app that
  asks for `media_backend="no_media"` (dj_reactor and standby do) makes the
  SDK release the media, and the daemon stops its media server: no camera,
  socket, mic levels or WebRTC until it is acquired again. The dashboard
  shows the camera and mic as unavailable meanwhile; that is the app
  holding them, not a fault. Upstream re-acquires only from the app's own
  `ReachyMini.__exit__`, so an app that crashes or is killed leaves it
  released, and `Daemon.start` keeps the release across a power cycle.
  **Bridge feature** (`features/media_return.py`): when an app's process
  ends and no newer app runs, a running daemon re-acquires the media; a
  stopped one forgets the release, so its next start brings media up.
- Since 1.14.0 the media server restarts its pipeline (stop, then a
  rebuild) after a pipeline error, at most 3 times in 60 s; WebRTC clients
  reconnect. The rebuild goes through `_configure_webrtc` again, so the
  bridge's signalling-host patch still applies.

## Daemon state and power

Endpoints: `daemon/app/routers/daemon.py`; implementation:
`daemon/daemon.py`.

| Call | Effect |
|---|---|
| `POST /api/daemon/start?wake_up=false` | Starts the backend without touching the motors. No motion. |
| `POST /api/daemon/start?wake_up=true` | Starts the backend, sets motor mode `enabled`, then runs `wake_up()`. |
| `POST /api/daemon/stop?goto_sleep=true` | Runs `reset_to_sleep()` (see below; it first stops a running move), then stops the backend. |
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
- **Stop with the sleep move during an idle reset ends in `error`** (since
  1.14.0). Nothing on the stop path cancels a running idle reset. Its
  `reset_to_sleep()` stops the idle reset's move, which leaves the rest of
  that sleep sequence's gotos returning at once while `goto_sleep()` keeps
  the move guard through its final wait, then fails on the guard with
  `MoveBusyError`. The state becomes `error` ("a move is already running")
  and the backend stops without the sleep move having finished; the head
  can be left mid-way, and the idle reset's own torque cut may come before
  or after the backend closes. This happens when the robot is powered off
  within about 1.5–8 s of an app exiting on its own or a remote session
  dropping, if the caller sends `goto_sleep=true` while the mode still
  reads `enabled`. The controller waits the reset out first (rule 8, #326).
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
  (`AnalyticalKinematics` by default, or `Placo`; 1.14.0 removed `NN`).
  `daemon/start` reads it from `app.state.args` each time, so it can only
  change while the backend is down.
- Under gravity compensation the head and antenna motors run in current
  control and **ignore every move target**: gotos, expressions and an app's
  moves are accepted and do nothing, while audio carries on. The controller
  refuses it while an app runs and switches back to `enabled` before starting
  one (#150).
- Gravity compensation needs Placo. Under any other engine,
  `motors/set_mode/gravity_compensation` is refused with `409` (`Gravity
  compensation requires the Placo kinematics engine.`) and nothing changes.
  1.12.0 answered a bare 500.
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
- `reset_to_init()` (since 1.14.0; used by `reset_to_sleep()` and an app
  stop): stops a move still running, quiesces head tracking and speech
  wobble, sets the motors to `enabled` (clearing gravity compensation and
  per-motor torque overrides), and lifts to the init pose (0.3–1.5 s).
- `reset_to_sleep()` (used by `daemon/stop?goto_sleep=true` and the idle
  reset): stops a running move, then, holding the move guard,
  `reset_to_init()`, 0.2 s, `goto_sleep()`. From a limp robot this looks
  like a wake followed by a sleep.
- **A move can fail on its own clock.** `play_move` (every goto, the wake-up
  and sleep moves) and `goto_joint_positions` check `time.time() - t0 <
  duration`, then read the clock again for `t`. A thread switch between the
  two reads near the end of a move gives `t > duration`, and
  `time_trajectory` raises `time value is out of range [0,1]`. In a
  `daemon/start?wake_up=true` that leaves the state `error` with the backend
  up and the wake-up cut short (#318: seen once in the sim browser tests).
  **Bridge feature** (`features/move_clock.py`): a time past the end is
  clamped to 1, so the move finishes at its target. Recorded moves
  (`RecordedMove.evaluate`) have the same race; it can end an expression a
  frame early with a failed move (a `move_failed` notification on the HTTP
  route the controller uses), but not fail a power-on.
- **Bridge feature** (`features/wobble.py`): the sleep and reset moves stop
  the wobble the dashboard turned on, and it comes back when the outermost
  one ends (`goto_sleep`, `reset_to_sleep` and `reset_to_init` are wrapped;
  they nest).
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
  (`/tmp/reachy_mini_sounds`), and since 1.14.0 answers `404` when the
  file is found nowhere (`500` if playback can't start). Move sounds are
  absolute paths into the dataset's Hugging Face cache; an uploaded move's
  audio is deleted when the move ends. A new sound stops the one playing. With
  `--no-media` there is no
  media server and nothing plays. **Bridge feature** (`features/sim_sounds.py`,
  sim daemons only, #219): every `play_sound` of a file in those folders
  (checked again when served), and every `stop_sound`, is recorded; `GET /api/bridge/sounds` lists the latest
  events and `GET /api/bridge/sounds/{token}` serves a played file by the
  random token its event names. The controller relays them to the dashboard in sim
  mode (`/api/sim/sounds-stream`, `/api/sim/sounds/{token}`).
- **One move at a time** (since 1.14.0). A move started while another runs
  raises `MoveBusyError` ("a move is already running"), including a sibling
  task on the same event loop. `wake_up()` (unless it returns early) and
  `goto_sleep()` hold the guard across their whole sequence, `goto_sleep`
  until the torque cut. A REST move still gets a UUID at once; the rejected
  one then ends with `move_failed` on `/api/move/ws/updates` and leaves
  `move/running`, so a UUID leaving `move/running` doesn't mean the move
  ran. The guard of a daemon-internal sequence (the idle reset, an app
  stop's `reset_to_init`) doesn't show in `move/running`. The controller
  still serializes its own power and wake actions (#148).

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
- A remote session can manage apps over the WebRTC data channel, without
  the controller (`daemon/jsonrpc_relay.py`): `apps.install` (older), and
  since 1.13.0 also `install_space`, `remove`, `update`, `restart`,
  `list_installed`, `check_update` and `job_status`, and since 1.14.0
  `get_startup` and `set_startup`. On a running backend, `set_startup`
  saves the choice and arms it as the loopback REST route does: the next
  `wake_up()` (even one that returns early) starts that app, and an
  antenna touch on the sleeping robot enables the motors, wakes it and
  starts the app, without the controller, until the daemon process
  restarts (`--no-autostart` only stops arming it at boot). `remove` of
  the startup app now clears it instead of refusing. The new verbs refuse
  while any registered job runs, REST-started ones included, but the REST
  app routes don't check for the relay's jobs, and the older inline
  `apps.install` isn't a registered job, so a controller install can
  overlap a remote one. Accepted by the owner for 1.14.0 (#292): an app
  started this way shows as running in the controller, which stops it on
  power off.

## Idle reset

Source: `request_idle_reset` and `_async_idle_reset` in
`daemon/backend/abstract.py`; wired in `Daemon.start`.

- **Trigger:** every transition of the slot to `free`, except a requested
  stop: since 1.14.0 the app monitor releases with `cleaned_up=True` once
  `stop-current-app` has begun, and `stop_current_app` resets the robot
  itself. That leaves an app exiting on its own (clean exit, crash, kill)
  and a remote session ending or dropping.
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
  `stop-current-app`. Upstream skips it there since 1.14.0, so this is now
  belt and braces. It doesn't block `stop_current_app`'s own fallback,
  which calls `request_idle_reset()` directly.
- **Not waited for by either stop path.** `daemon/stop?goto_sleep=true` and
  `stop-current-app` don't cancel or wait for a running idle reset. On 1.13.0
  they run a competing move (`stop-current-app`'s return home, or a second
  `reset_to_sleep`) and `daemon/stop` then tears the backend down under the
  reset. On 1.14.0 they stop its move part-way: the head can stay mid-pose,
  or drop when the sleep move's torque cut lands where it stopped, and the
  stop fails on the move guard with the daemon in `error` (#292). No upstream
  endpoint shows the reset (`is_move_running` reaches only WebRTC clients,
  and `move/running` lists only REST moves). The controller waits for it
  instead (rule 8 under Rules for the controller, #326).
- **Bridge route (`features/idle_reset.py`, #292):** `GET
  /api/bridge/idle-reset` answers `{"pending": true}` from the reset's grace
  period until it ends. It reads `_idle_reset_task` on the backend's loop,
  where `request_idle_reset` schedules it with `call_soon_threadsafe` in the
  same step that marks the app `done` or `error`, so a read after the
  controller sees the exit can't miss a reset about to start. No backend or
  no loop: `false`. A loop that doesn't answer within 0.5 s: `true`, so a
  wedged backend loop holds power off for the full timeout and refuses app
  starts until the daemon restarts. A runtime whose `idle_reset` feature was
  skipped (a `--check` mismatch) answers 404, which the controller reads as
  nothing pending: the protection is then off.

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
  - In every case the slot is released. That triggers the idle reset,
    except for a requested stop (see Idle reset).
- **A crashed app blocks every start** until `POST
  /api/apps/stop-current-app` clears it. On an `error` app that call does not
  signal a process (it has already exited). It still runs the return-to-home
  step below, so it can move the robot. On a `done` or `stopping` app it
  returns `400 No app is currently running`.
- **Requested stop** (`stop-current-app`): SIGINT, then SIGKILL of the process
  tree after 20 s. Then:
  - First, since 1.14.0, it stops a move still running (`stop_running_move`,
    waiting up to 1 s).
  - Head within the sleep-pose radius: sets the motors to `disabled`.
  - Otherwise: `reset_to_init()` (see Moves): motors `enabled`, gravity
    compensation, tracking and wobble cleared, lift to the init pose over
    0.3–1.5 s. The robot stays awake there. If that fails, it falls back
    to `request_idle_reset()`.
  - **Clearing a crashed app during its idle reset can drop the head.**
    Roughly 1.5–6.5 s after a crash the idle reset's `reset_to_sleep()`
    is running. The first step above sets the stop flag, which nested
    moves keep, so every remaining goto of that sleep sequence returns at
    once, while `goto_sleep()` still waits and then sets the motors to
    `disabled` wherever the head is. The stop's own `reset_to_init()`
    meanwhile enables the motors and fails with `MoveBusyError`, so the
    fallback idle reset runs about 1.5 s later. 1.13.0's stop didn't
    interrupt the sleep move. The controller clears an `error` app before
    each start (rule 3), and before that waits for the idle reset to end
    (rule 8, #326), so its Start and Stop don't take this path.
  - **Bridge patch:** the keep-awake wrapper sets the mode to `enabled`
    after the stop when torque is on and the mode says `disabled`. Written
    for 1.13.0's `enable_motors()` path, which left the mode as it was;
    `reset_to_init()` sets it, so this no longer triggers there.
- **Startup app** (`GET` and `PUT /api/apps/startup-app`, saved in the
  user's `reachy_mini/daemon_config.json`, which the robot's daemon and a
  sim daemon of the same user share). **A power-on over REST never starts
  it.** The daemon arms it at boot only with `--autostart`, and
  `daemon/start` builds each backend without a wake hook. Checked on
  1.14.0 in the sim: with the startup app set while the robot is off, and
  again set while it is on and then powered off and on, no app started.
  A `PUT` on a running backend arms that backend's next `wake_up()` only,
  and starts the antenna watcher described under "Robot slot", which acts
  without the controller. The bridge doesn't call these routes: the
  controller keeps its own switch and starts the app after its power-on
  (rule 9, #334).
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
  (#158). Some apps pick a port the bridge uses (dj_reactor serves on
  `DJ_UI_PORT`, default 7861, and leaves `custom_app_url` unset). **Bridge
  patch (#274):** the shim exits a process that tries to serve on a port in
  `REACHY_BRIDGE_PORTS` (set by the daemon units; the same list in robot
  and sim mode) with a `reachy-bridge: ` line, which the controller shows
  as the app's error: at once if the port is free, else after 10 s unless
  the process listens on another port by then (port finders like Gradio's
  probe each port with a bind and move on).
- All apps share one `apps_venv`, so one app's loose requirement sets a
  library's version for all of them: reachy_baby_yoda allows any OpenCV
  from 4.5, uses `cv2.CascadeClassifier` and the Haar cascade files, and
  OpenCV 5 (pulled in by another install) has neither. **Bridge feature
  (#279, `features/app_overlays.py`):** `install.sh` builds an OpenCV 4
  overlay folder (`uv pip install --target`, no dependencies) under
  `app-overlays/`. Before each start the bridge checks the app's
  `cv2.<name>` uses against the venv's OpenCV; an app missing any, or one
  that exits with `module 'cv2' has no attribute`, is recorded in
  `<state dir>/app-overlays.json`, and its processes get the overlay ahead
  of the venv's packages (the shim reads `REACHY_BRIDGE_APP_PATHS`). Other
  apps keep the venv's OpenCV. A scan result is dropped once the venv's
  OpenCV has every name again; a crash result stays until its entry is
  deleted. Not caught: indirect uses through another library that
  don't fail with that error, and calls that still exist but changed.
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
  app base class in reachy-mini 1.12.0 (unchanged in 1.13.0 and 1.14.0),
  Radio, Conversation App and Gradio's Python server send neither, and the
  proxy passes app headers through as they are. It strips the controller's own
  cookies (both ways), the local
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
8. Before `daemon/stop` or `stop-current-app`, and before waking the robot
   for an app, wait until `bridge/idle-reset` reports no reset pending (up to
   30 s, which covers the 15 s hand-off grace), then read the robot state
   again: an asleep robot gets `goto_sleep=false` (rule 5). Power off goes
   ahead after the timeout; app start and stop are refused. With no reset
   pending (a requested stop, a robot already asleep) each check is one
   read. Power off stops a `starting` or `running` app at once (it holds
   the robot, so no reset is pending, whatever the route says), checks
   before stopping an app it can't read (stopped as if running), and checks
   again after the stop (1.14.0's fallback) unless the first wait timed out,
   so a stuck reset costs one timeout. An app
   stop waits only for an `error` app or one it can't read: a running app
   holds the robot, and with no app or a `done` one the daemon stops
   nothing. After any wait, read the app again before acting on it.
9. To start an app when the robot wakes, start it from the controller after
   its own power-on, through the same guards as any app start (rule 3, rule
   8). Don't set the daemon's startup app: it doesn't start on a REST
   power-on, and once armed its antenna watcher checks only the daemon's
   own app and robot slot, which the controller's expressions and drive
   sessions don't hold.
