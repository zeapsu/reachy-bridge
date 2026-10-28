# controller

The reachy-bridge controller (#144): one Rust binary (axum + tokio) that serves the dashboard
and the controller API in front of the local Reachy daemon. The dashboard is built from `web/`
and embedded at compile time from `web/dist`.

## Build and run

```bash
(cd ../web && bun install && bun run build)   # the dashboard, into web/dist
cargo build --release                          # embeds web/dist
DAEMON_URL=http://127.0.0.1:8000 target/release/reachy-controller
```

Without `web/dist` the binary still builds and serves the API; `/` then says to build the UI.

| Variable | Default | |
|---|---|---|
| `DAEMON_URL` | `http://127.0.0.1:8000` | daemon base URL (loopback only) |
| `DAEMON_TIMEOUT` | `2.0` | default per-request timeout, seconds |
| `CONTROLLER_HOST` / `CONTROLLER_PORT` | `127.0.0.1` / `7861` | listen address; only loopback unless `CONTROLLER_LAN=1` |
| `CONTROLLER_APP_PORT` | `7863` | app UI proxy port, always on `127.0.0.1` |
| `REACHY_DAEMON_UNIT` | `reachy-bridge.service` | user unit whose journal feeds the `daemon` log source |
| `REACHY_BRIDGE_STATE_DIR` | `$XDG_STATE_HOME/reachy-bridge` | favorites and `logs/controller.log` |
| `RUST_LOG` | `info` | log filter |

## Tests

```bash
cargo test                 # unit tests and API tests against a scripted fake daemon
cargo clippy --all-targets
```

Against a real sim daemon, run `tools/sim/smoke.py` with `SIM_CONTROLLER` pointing at this
binary (see `tools/sim/README.md`).

## Layout

- `daemon.rs`: daemon HTTP client and robot-state derivation (`docs/daemon-contract.md`)
- `routes/power.rs`: power on/off, wake before apps, kinematics switch, all under the power lock
- `routes/apps.rs`, `apps.rs`: app list, start/stop, UI probe, favorites
- `routes/controls.rs`: volume, motor mode (gravity-compensation guards), wobble, sounds
- `expressions.rs`: one expression at a time, then a return to the home pose
- `streams.rs`, `routes/telemetry.rs`: shared state/audio/stats upstreams and their SSE routes
- `app_proxy.rs`: the second listener, a root-path HTTP and WebSocket proxy to the running
  app's own UI, so it can be served over HTTPS (#158)
- `routes/camera.rs`: MJPEG preview proxy; `logs.rs`, `routes/logs.rs`: log sources and SSE

Unlike the stopgap, there is no catch-all proxy to the daemon: the controller exposes only the
routes the dashboard uses. The app UI proxy forwards only to the current app's UI port, which it
reads from the daemon, never from the request.
