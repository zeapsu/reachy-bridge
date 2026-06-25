# AGENTS.md — Reachy Mini Lite workspace

Project memory for agents/humans working in `~/reachy`. For end-user usage see
`README.md` and `docs/USER_GUIDE.html`.

> Canonical agent guide. `CLAUDE.md` is a symlink to this file, so Claude Code, Codex, and other
> AGENTS.md-aware harnesses all read the same content.

## What this is
A **Reachy Mini Lite** (Pollen Robotics / Hugging Face) workspace, built **simulation-first**.
The same `ReachyMini()` code runs against the sim daemon and the real robot — you only change
which daemon is running. Custom apps live in `apps/`. The upstream SDK clone (`reachy_mini/`) and
reference app (`reachy_mini_conversation_app/`) are gitignored.

## Environment & commands
- **Use `uv run`** — do not call `.venv/bin/python` directly or activate venvs.
  From an app dir, `uv run python -m <pkg>.main` builds/uses that app's own env.
- Python 3.12, `reachy-mini` 1.8.3, `mujoco` 3.3.7.
- **Daemon** (must be running; bridges code ↔ motors/sensors, serves `:8000`):
  - Simulation: `uv run python -m reachy_mini.daemon.app.main --sim --headless`
  - **Jetson hardware (current host — Lite over USB): `uv run python tools/reachy_mini_daemon_jetson.py`**
    (NOT plain `reachy-mini-daemon` — the wrapper aliases `unixfd*→nvunixfd*` so the camera socket
    is created; stock launcher = no camera). See "Jetson / Ubuntu 24.04 gotchas" below.
  - Other real hardware (non-Jetson, Lite over USB): `uv run reachy-mini-daemon`
- Run an app: `cd apps/<app> && uv run python -m <app>.main` (e.g. `reachy_dance.main`).
  Only **one app uses the robot at a time** — stop the current one first:
  `curl -X POST localhost:8000/api/apps/stop-current-app`. **That endpoint only stops
  daemon-managed apps;** an app you launched directly with `uv run python -m <app>.main` returns
  `{"detail":"No app is currently running"}` — stop those with **Ctrl-C** instead.

## Apps (`apps/`)
- `reachy_dance` — beat-synced head/antenna choreography + idle breathing.
- `reachy_simon` — Simon-style memory game (antennas as buttons).
- `reachy_lookat` — OpenCV face-tracking head control loop.
- `reachy_chat` — voice: mic → faster-whisper (local) → OpenRouter → macOS `say` (local) → speaker.
- `reachy_controller` — FastAPI + **vanilla-JS, no build step** "Liquid Glass" web control console
  (binds `127.0.0.1:7861`). Talks to the daemon over HTTP/WS and **never** instantiates `ReachyMini()`.
  Don't add a framework/bundler. Visual QA is done on a browser-capable machine (see Dev workflow).
  A **daemon power button** in the viewport inset (`#vp-power`) starts/stops the **daemon process**
  (`DaemonSupervisor` + `/api/daemon/{status,start,stop}`) so you never have to SSH in to start it —
  it manages a *process*, still never the robot. Gated by `REACHY_MANAGE_DAEMON=1` (**on-device only**;
  a Mac-tunnel controller can't manage a remote daemon, so the button disables itself). The daemon is
  spawned detached (`start_new_session=True`) and kept out of the controller's `stop_all`, so it
  survives controller restarts. Authoritative on/off = the daemon's `:8000` reachability.

## Hard-won gotchas (macOS / Apple Silicon) — READ before debugging
- **No MuJoCo GUI** on this Mac → always use `--sim --headless` for simulation. Headless is enough
  to verify apps.
- **mujoco must be 3.3.7.** The 3.2.x series throws an `unrecognized attribute: site1` XML error
  on the Reachy model.
- **SDK/daemon versions must match.** The Reachy *desktop app* bundles an older daemon (was 1.8.0)
  while our env is SDK 1.8.3 — the mismatch breaks the audio pipeline. Fix: quit the desktop app
  and run our own `uv run reachy-mini-daemon` (1.8.3), or otherwise align versions.
- **Microphone — both causes below were diagnosed and RESOLVED on this unit (2026-06-21):
  mic permission granted + ribbon reinstalled correctly; mic is confirmed working. Treat these
  as historical reference, NOT current suspects — only revisit if `mic_test.py` shows RMS = 0 again.**
  - The terminal app (Ghostty) needs macOS **mic permission** (System Settings → Privacy &
    Security → Microphone); a denied stream returns correctly-shaped buffers full of **zeros**.
  - The robot's mic **ribbon cable can be installed reversed** → also pure silence. Symptom: device
    "Reachy Mini Audio" present, frames flow, but RMS = 0.
  - Diagnose with `uv run python mic_test.py` (RMS probe) or, independent of the daemon,
    `ffmpeg -f avfoundation -i ":0" -t 5 -af volumedetect -f null -` (run with daemon OFF so the
    device is free). If `ffmpeg` also shows silence the fault is below the daemon (permission/ribbon);
    if `ffmpeg` has signal but the SDK path is silent, suspect XVF3800 DSP state (next bullet).
  - **DSP-state mute:** ad-hoc / out-of-sequence `PP_*` control writes (via
    `python -m reachy_mini.media.audio_control_utils <PARAM> --values ...`) can leave the XVF3800
    DSP in a bad state → frames flow but RMS = 0, even though permission + hardware are fine.
    Recover with a soft reset: `uv run python -m reachy_mini.media.audio_control_utils REBOOT --values 1`
    (restores factory defaults, e.g. `PP_MGSCALE` first value 1000.0). The app's own
    `AUDIO_STARTUP_CONFIG` (incl. `PP_MGSCALE=4.0`) is fine — verified mic RMS ~0.2 after a normal app start.
- **`reachy_chat` TTS:** pyttsx3's `save_to_file` is broken on Apple Silicon (`nsss` driver) →
  produces no decodable audio. We synthesize with the native macOS **`say`** command instead
  (pyttsx3 kept as the non-macOS fallback). See `src/reachy_chat/openrouter_chat.py`.
- **`reachy_chat` LLM:** OpenRouter (OpenAI-compatible), default model `minimax/minimax-m3`
  (multimodal: text/image/video). Visual questions attach the current camera frame;
  `OPENROUTER_MODEL` overrides the default. Key comes from `OPENROUTER_API_KEY` in the **environment**,
  OR the app's Settings UI at `http://localhost:7860` (persists to a per-instance `.env`). A plain
  `.env` dropped in the app folder is **not** auto-loaded. First run downloads faster-whisper
  `base.en` (~200 MB). Talk, then **pause ~0.8 s** to finalize a turn.
- **Camera exposure is NOT controllable** via the SDK/daemon on macOS — AVFoundation
  (`avfvideosrc`) doesn't expose exposure/gain to GStreamer, and the daemon only serves read-only
  `/api/camera/specs`. (Exposure via v4l2 is only available on the Linux/wireless robot.)

## Hard-won gotchas (Jetson / Ubuntu 24.04) — current host, READ before debugging
The workspace now runs on an **NVIDIA Jetson Orin Nano**, migrated 2026-06-23 to **Jetpack 7.2 /
Ubuntu 24.04 (GStreamer 1.24.2)**. The macOS section above is stale for this host.
- **Launch the daemon via the Jetson wrapper** (`uv run python tools/reachy_mini_daemon_jetson.py`),
  never stock `reachy-mini-daemon`. The wrapper aliases `unixfd*→nvunixfd*`; without it the camera
  IPC socket `/tmp/reachymini_camera_socket` is never created → no camera frames.
- **`reachy_chat` uses the LOCAL media backend** (`REACHY_MINI_MEDIA_BACKEND=local`, default in
  `main.py`), NOT WebRTC. The daemon's `GstWebRTCSink ... 'run-signalling-server'` error at startup
  is therefore **harmless** — the custom `libgstrswebrtc.so` (0.10.11, built vs GStreamer 1.20) is
  version-mismatched under 1.24.2, but the app never touches WebRTC. Only matters for remote streaming.
- **Client processes must call `apply_nvidia_unixfd_patch()` before connecting** (the app does at
  `main.py:26`). Standalone SDK scripts that skip it get `Internal data stream error` on the camera.
- **`dialout` group:** after an OS reinstall the user can lose `dialout` → daemon finds
  `/dev/ttyACM0` but dies with `Failed to start daemon: Permission denied`. Fix:
  `sudo usermod -aG dialout <user>` then **reboot** (restarting just the terminal does NOT refresh
  groups — verify with `grep Groups /proc/<daemon-pid>/status`; dialout gid is 20).
- **DOA needs a udev rule for the mic-array USB device (RESOLVED 2026-06-24).** The daemon reads
  Direction-of-Arrival via raw **libusb** control transfers on the XVF3800 (`Pollen Robotics Reachy
  Mini Audio`, USB `38fb:1001`). Speaker/mic work without it (kernel USB-audio driver, `audio` group),
  but the `/dev/bus/usb` node defaults to `root:root 0664`, so libusb DOA reads get
  `usb.core.USBError: [Errno 13] Access denied` → `GET /api/state/full?with_doa=true` returns **500**,
  and the controller's telemetry + audio-level streams then **falsely report the daemon offline**
  (the daemon is fine; only DOA failed). Fix is checked in: `tools/udev/99-reachy-mini-doa.rules`
  (grants `plugdev` access). Install: `sudo cp tools/udev/99-reachy-mini-doa.rules /etc/udev/rules.d/
  && sudo udevadm control --reload-rules && sudo udevadm trigger` (recurs after an OS reinstall, like
  the dialout fix). DOA reads are per-request, so no daemon restart needed once perms change.
- **Speaker silence was a VOLUME/routing bug (RESOLVED 2026-06-23), not a pipeline issue.**
  The app always pushed valid audio; it was just inaudibly quiet. Two stacked causes:
  (1) **ALSA card numbering is NOT stable** — after the migration the Reachy USB audio moved
  to **card 0** (NVIDIA APE took card 2). The wrapper used to hardcode `amixer -c 2`, so it set
  volume on the wrong card and logged `Unable to find simple control 'PCM'`. The wrapper now
  discovers the Reachy card by name (`_find_reachy_audio_card()` in
  `tools/reachy_mini_daemon_jetson.py`) and sets `PCM,1` to 100%.
  (2) The **PipeWire** Reachy sink defaults to ~0.40 — raise it with
  `wpctl set-volume @DEFAULT_AUDIO_SINK@ 1.0` (WirePlumber persists per-device state).
  Verify hardware vol: `amixer -c 0 sget 'PCM,1'`; verify routing/level: `wpctl status`.
  Re-verify audibly after any OS/driver migration — don't trust pre-migration tone proofs.
- **Smoke test** for vision/mic/speaker: `apps/reachy_chat/scratchpad/migration_smoke_test.py`
  (run with `REACHY_MINI_MEDIA_BACKEND=local`). Vision + mic + speaker verified working post-migration.

## Dev workflow
Implementation is delegated to the **Codex CLI** (`codex exec --yolo "<prompt>"`) and verified by an
Opus review loop (see the `codex-implement-review-loop` agent). If `codex exec` hits a
credit/quota/auth limit, **pause** and re-auth (`codex logout`/`login`) — do not retry or hand-write
the code. Diagnostic scripts live at the repo root (`mic_test.py`).

### Issue-driven workflow (adopted 2026-06-24)
For multi-session work, **open GitHub issues for what's next** (a parent/epic issue + child issues)
and update **memory + this AGENTS.md** in the same pass — issues track *what's next*, memory/AGENTS.md
track *what we learned*. PRs reference their issues (`Closes #N`) and may ship in parts (e.g. part 1
visual/QA → part 2 functional verification). Repo: `zeapsu/reachy`; PR base is the default branch
`feat/reachy-chat-vision`. End sessions with `/close` (do **not** recreate `SESSION_LOG.md` — removed,
git history + memory replace it).

### reachy_controller UI gotchas
- **Visual QA runs on a browser-capable machine** (Mac/laptop w/ Playwright), not the headless Jetson.
  Serve via `uv run python -m reachy_controller.main`; daemon-offline panels are the expected state.
  A **local, gitignored** Mac dev helper (`*.local.sh`, e.g. `dev_controller_mac.local.sh`) opens an
  SSH tunnel to the Jetson daemon and runs the controller locally for UI/visual QA (#22). **Caveat:**
  to QA the *on-device daemon power button*, the controller must run **on the Jetson** with
  `REACHY_MANAGE_DAEMON=1` (a local controller only manages a local daemon); for that, tunnel the
  **controller** port instead (`ssh -N -L 7870:localhost:7861 <jetson>`, a distinct local port to
  dodge a local :7861) and point the browser at `:7870`.
- **Never put `filter: url(#…)` (feDisplacementMap) on `.vp-stage`** — it warps the live camera/sim/
  schematic *content* (the original "distortion" bug). Liquid-glass refraction belongs on the panel
  **backdrop** (`backdrop-filter: url(#lg-refract)`, behind `@supports`) — Chromium-only, with a
  CSS-only rim/frost fallback for Safari/Firefox. Verify backdrop refraction in the actual kiosk browser.
