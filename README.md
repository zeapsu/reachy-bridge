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
│   ├── reachy_dance/            # beat-synced head + antenna choreography
│   ├── reachy_simon/            # Simon-style memory game (antennas as buttons)
│   ├── reachy_lookat/           # camera face-tracking head control loop
│   └── reachy_chat/             # voice conversation: STT → OpenRouter → TTS
├── docs/USER_GUIDE.html         # comprehensive guide (this is the deliverable)
├── reachy_mini/                 # upstream SDK clone (reference)               [gitignored]
└── reachy_mini_conversation_app/# upstream reference app                       [gitignored]
```

## Quick start

```bash
# 1. Start the daemon
#    Simulation (no robot, headless — this Mac has no MuJoCo GUI):
~/reachy-console/.venv/bin/python -m reachy_mini.daemon.app.main --sim --headless
#    Real hardware (Lite over USB):
reachy-mini-daemon

# 2. Smoke test
~/reachy-console/.venv/bin/python ~/reachy-console/hello.py

# 3. Run an app
cd ~/reachy-console/apps/reachy_dance && ~/reachy-console/.venv/bin/python -m reachy_dance.main
```

Dashboard + API: <http://localhost:8000/> · API docs: <http://localhost:8000/docs>

## The apps

| App | What it does | Verified | Needs hardware for |
|-----|--------------|----------|--------------------|
| `reachy_dance` | Rhythmic choreography + idle breathing | sim ✅ | physical motion only |
| `reachy_simon` | Memory game; motion cues + auto-mode | sim ✅ | antenna/head **input** |
| `reachy_lookat` | OpenCV face tracking → head steering | sim ✅ | **camera** |
| `reachy_chat` | mic → faster-whisper → OpenRouter (`deepseek/deepseek-v4-flash`) → pyttsx3 → speaker | structure ✅ | **mic/speaker** + API key |

`reachy_chat` needs `OPENROUTER_API_KEY` in `apps/reachy_chat/.env` (copy from `.env.example`).
First run downloads the faster-whisper `base.en` model (~200 MB).

See the [full guide](docs/USER_GUIDE.html) for details, hardware migration, and troubleshooting.
