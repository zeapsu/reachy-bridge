# reachy-bridge

Run a **Reachy Mini Lite** from a **headless Linux edge device**, so the robot stays available
without a laptop plugged in. The bridge runs the upstream Reachy Mini daemon as an always-on
service, serves a controller UI you can reach from your other devices, and installs community
apps from Hugging Face Spaces.

> **Status:** in active development; not yet ready for general use. Tested on a Jetson Orin
> Nano running JetPack 7. Other Linux boards are experimental. Progress is tracked in
> [the release epic](https://github.com/zeapsu/reachy-bridge/issues/111).

## What's here today

```
apps/reachy_controller/   controller: FastAPI backend + browser UI (stopgap, see #144)
tools/                    Jetson daemon wrapper, media extensions, webrtcsink build, udev rule
hello.py                  SDK smoke test (antenna wiggle)
```

The controller talks to the daemon at `127.0.0.1:8000`. It switches the robot on and off,
plays expressions, drives the head and antennas, shows telemetry, camera, audio and logs,
and starts or stops installed Hugging Face apps. A running app is opened in its own UI.

## Development quick start

```bash
uv sync --all-packages
uv run python -m reachy_mini.daemon.app.main --sim --headless   # simulator daemon
cd apps/reachy_controller && uv run python -m reachy_controller.main
```

Then open <http://127.0.0.1:7861/>. Run the tests with `uv run pytest apps/reachy_controller tools`.

A user-facing install guide, `CONTRIBUTING.md` and the license arrive with the public release
(#135, #136, #60).
