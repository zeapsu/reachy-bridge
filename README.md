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
runtime/                  Pollen's daemon plus the bridge's features and platform adapters
controller/               controller: Rust (axum) binary that embeds the dashboard (#144)
web/                      dashboard: React + TypeScript, built with Vite and Bun
deploy/                   installer, user units, and the reachy-bridge command
tools/                    sim smoke test, webrtcsink build for Jetson, udev rule
hello.py                  SDK smoke test (antenna wiggle)
```

The controller talks to the daemon at `127.0.0.1:8000`. It switches the robot on and off,
plays expressions, shows telemetry, camera, audio and logs,
and starts or stops installed Hugging Face apps. A running app is opened in its own UI.

The daemon is Pollen's `reachy-mini`, tracked rather than pinned: `uv.lock` holds the tested
version and is bumped as Pollen releases (#155). Everything the bridge patches in the daemon
goes through `runtime/src/reachy_bridge_runtime/upstream.py`, and
`reachy-bridge-runtime --check` verifies those hooks against the installed version.

## Install on the device

```bash
deploy/install.sh        # runtime venv, controller, user units, the reachy-bridge command
reachy-bridge serve      # publish on the tailnet (dashboard on HTTPS :10000)
reachy-bridge status
```

The robot starts off; switch it on from the dashboard. `install.sh` is safe to re-run after a
pull, and never restarts the daemon while the robot is on.

## Development quick start

```bash
uv sync
uv run reachy-bridge-runtime --sim --headless --fastapi-port 8000   # simulator daemon
(cd web && bun install && bun run build)                             # dashboard into web/dist
(cd controller && cargo run --release)                                # controller on :7861
```

Then open <http://127.0.0.1:7861/>. For UI work, `bun run dev` in `web/` serves the dashboard
with hot reload and proxies the API to the controller. Tests: `cargo test` in `controller/`,
`bun test` in `web/`, `uv run pytest` (runtime), and `tools/sim/smoke.py` against a sim daemon.
CI runs everything but the sim smoke test on each PR, on `ubuntu-24.04-arm` (GStreamer 1.24.2,
as on JetPack 7). Pushing a `vX.Y.Z` tag runs CI and then publishes a GitHub Release with the
source archive, `uv.lock`, `SHA256SUMS` and a build attestation; `-rc` tags are prereleases.

A user-facing install guide, `CONTRIBUTING.md` and the license arrive with the public release
(#135, #136, #60).
