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
  - Simulation on this Mac: `uv run python -m reachy_mini.daemon.app.main --sim --headless`
  - Real hardware (Lite over USB): `uv run reachy-mini-daemon`
- Run an app: `cd apps/<app> && uv run python -m <app>.main` (e.g. `reachy_dance.main`).
  Only **one app uses the robot at a time** — stop the current one first:
  `curl -X POST localhost:8000/api/apps/stop-current-app`.

## Apps (`apps/`)
- `reachy_dance` — beat-synced head/antenna choreography + idle breathing.
- `reachy_simon` — Simon-style memory game (antennas as buttons).
- `reachy_lookat` — OpenCV face-tracking head control loop.
- `reachy_chat` — voice: mic → faster-whisper (local) → OpenRouter → macOS `say` (local) → speaker.

## Hard-won gotchas (macOS / Apple Silicon) — READ before debugging
- **No MuJoCo GUI** on this Mac → always use `--sim --headless` for simulation. Headless is enough
  to verify apps.
- **mujoco must be 3.3.7.** The 3.2.x series throws an `unrecognized attribute: site1` XML error
  on the Reachy model.
- **SDK/daemon versions must match.** The Reachy *desktop app* bundles an older daemon (was 1.8.0)
  while our env is SDK 1.8.3 — the mismatch breaks the audio pipeline. Fix: quit the desktop app
  and run our own `uv run reachy-mini-daemon` (1.8.3), or otherwise align versions.
- **Microphone:**
  - The terminal app (Ghostty) needs macOS **mic permission** (System Settings → Privacy &
    Security → Microphone); a denied stream returns correctly-shaped buffers full of **zeros**.
  - The robot's mic **ribbon cable can be installed reversed** → also pure silence. Symptom: device
    "Reachy Mini Audio" present, frames flow, but RMS = 0.
  - Diagnose with `uv run python mic_test.py` (RMS probe) or, independent of the daemon,
    `ffmpeg -f avfoundation -i ":0" -t 5 -af volumedetect -f null -` (run with daemon OFF so the
    device is free).
- **`reachy_chat` TTS:** pyttsx3's `save_to_file` is broken on Apple Silicon (`nsss` driver) →
  produces no decodable audio. We synthesize with the native macOS **`say`** command instead
  (pyttsx3 kept as the non-macOS fallback). See `src/reachy_chat/openrouter_chat.py`.
- **`reachy_chat` LLM:** OpenRouter (OpenAI-compatible), default model `deepseek/deepseek-v4-flash`,
  overridable via `OPENROUTER_MODEL`. Key comes from `OPENROUTER_API_KEY` in the **environment**,
  OR the app's Settings UI at `http://localhost:7860` (persists to a per-instance `.env`). A plain
  `.env` dropped in the app folder is **not** auto-loaded. First run downloads faster-whisper
  `base.en` (~200 MB). Talk, then **pause ~0.8 s** to finalize a turn.
- **Camera exposure is NOT controllable** via the SDK/daemon on macOS — AVFoundation
  (`avfvideosrc`) doesn't expose exposure/gain to GStreamer, and the daemon only serves read-only
  `/api/camera/specs`. (Exposure via v4l2 is only available on the Linux/wireless robot.)

## Dev workflow
Implementation is delegated to the **Codex CLI** (`codex exec --yolo "<prompt>"`) and verified by an
Opus review loop (see the `codex-implement-review-loop` agent). If `codex exec` hits a
credit/quota/auth limit, **pause** and re-auth (`codex logout`/`login`) — do not retry or hand-write
the code. Diagnostic scripts live at the repo root (`mic_test.py`).
