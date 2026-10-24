# Sim smoke test

Exercises the controller <-> daemon flows against the real daemon in MuJoCo
sim mode, so behaviour bugs show up before a hardware QA pass. Only the
simulated robot moves.

```bash
uv sync   # the runtime, at the locked reachy-mini, with MuJoCo
uv run reachy-bridge-runtime --sim --headless --no-media \
    --desktop-app-daemon --no-autostart --no-wake-up-on-start --fastapi-port 8100 &
# install the test apps once
for spec in pollen-robotics/reachy_mini_radio panny247/hello_world; do
  curl -X POST localhost:8100/api/apps/install -H 'Content-Type: application/json' \
    -d "{\"name\":\"${spec#*/}\",\"source_kind\":\"hf_space\",\"url\":\"https://huggingface.co/spaces/$spec\"}"
done
(cd web && bun install && bun run build) && (cd controller && cargo build --release)
DAEMON_URL=http://127.0.0.1:8100 CONTROLLER_PORT=7862 REACHY_BRIDGE_STATE_DIR=/tmp/rb-sim \
    controller/target/release/reachy-controller &
uv run python tools/sim/smoke.py
```

The daemon puts community apps in `apps_venv/` next to the venv it runs from
(the repo root here, gitignored), so the test apps install once per checkout.

`hello_world` exits on start in a clean apps venv (missing `scipy`), which
the test uses as the crash case. With `--no-media` the daemon's idle reset
(sleep after a crash) never runs, so that check is skipped and stays a
hardware check. Becomes CI integration tests under #146 / #131.
