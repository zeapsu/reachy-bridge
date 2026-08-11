# Reachy Mini — Workspace

Software stack, imported reference programs, and custom apps for a **Reachy Mini Lite**
(Pollen Robotics / Hugging Face). Built **simulation-first** so development happens before the
hardware is connected; the same code runs on the real robot by switching the daemon.

> 📖 **Full user guide:** open [`docs/USER_GUIDE.html`](docs/USER_GUIDE.html) in a browser.

## Layout

```
~/reachy-console/
├── .venv/                       # uv venv (Python 3.12, reachy-mini 1.8.3, mujoco 3.3.7)  [gitignored]
├── hello.py                     # SDK smoke test (antenna wiggle)
├── apps/
│   ├── reachy_controller/       # on-device FastAPI + vanilla-JS console
│   ├── reachy_chat/             # configurable realtime/turn-based voice conversation
│   ├── reachy_hermes/           # local Whisper/TTS voice front end for Hermes
│   ├── reachy_dance/            # beat-synced head + antenna choreography
│   ├── reachy_simon/            # Simon-style memory game (antennas as buttons)
│   └── reachy_lookat/           # camera face-tracking head control loop
├── docs/USER_GUIDE.html         # comprehensive guide
├── reachy_mini/                 # upstream SDK clone (reference)               [gitignored]
└── reachy_mini_conversation_app/# upstream reference app                       [gitignored]
```

## Quick start

```bash
# 1. Install every workspace package in the shared environment
uv sync --all-packages

# 2. Start the headless simulator
uv run python -m reachy_mini.daemon.app.main --sim --headless

# 3. Smoke test the SDK
uv run python hello.py

# 4. Run an app
cd apps/reachy_dance && uv run python -m reachy_dance.main
```

Dashboard + API: <http://localhost:8000/> · API docs: <http://localhost:8000/docs>

## The apps

| App | What it does | Verified | Needs hardware for |
|-----|--------------|----------|--------------------|
| `reachy_controller` | Launches workspace apps, expressions, media, and robot controls | tests + browser ✅ | full controller integration |
| `reachy_dance` | Rhythmic choreography + idle breathing | sim ✅ | physical motion only |
| `reachy_simon` | Memory game; motion cues + auto-mode | sim ✅ | antenna/head **input** |
| `reachy_lookat` | OpenCV face tracking → head steering | sim ✅ | **camera** |
| `reachy_chat` | Configurable realtime or local-turn voice conversation | structure ✅ | **mic/speaker** + provider configuration |
| `reachy_hermes` | mic → faster-whisper → local Hermes CLI → KittenTTS (sherpa-onnx, int8) → speaker | unit ✅ | **mic/speaker** + local Hermes installation |

`reachy_chat` OpenAI Realtime setup belongs to Reachy Console's host credential
service; its controller settings are write-only and Reachy Chat receives a
short-lived provider capability rather than the durable key.
`reachy_hermes` requires `~/.local/bin/hermes` on the robot host.
First run downloads the faster-whisper `base.en` model (~200 MB) and the KittenTTS int8 model.

See the [full guide](docs/USER_GUIDE.html) for details, hardware migration, and troubleshooting.
