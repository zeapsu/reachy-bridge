# Sim smoke test

Exercises the controller <-> daemon flows against the real daemon in MuJoCo
sim mode, so behaviour bugs show up before a hardware QA pass. Only the
simulated robot moves.

```bash
uv venv --python 3.12 .sim && uv pip install --python .sim/bin/python "reachy-mini[mujoco]==1.12.0" -e apps/reachy_controller
.sim/bin/python tools/reachy_mini_daemon_jetson.py --sim --headless --no-media \
    --desktop-app-daemon --no-autostart --no-wake-up-on-start --fastapi-port 8100 &
# install the test apps once
for spec in pollen-robotics/reachy_mini_radio panny247/hello_world; do
  curl -X POST localhost:8100/api/apps/install -H 'Content-Type: application/json' \
    -d "{\"name\":\"${spec#*/}\",\"source_kind\":\"hf_space\",\"url\":\"https://huggingface.co/spaces/$spec\"}"
done
(cd apps/reachy_controller && DAEMON_URL=http://127.0.0.1:8100 CONTROLLER_PORT=7862 \
    REACHY_BRIDGE_STATE_DIR=/tmp/rb-sim ../../.sim/bin/python -m reachy_controller.main &)
.sim/bin/python tools/sim/smoke.py
```

`hello_world` exits on start in a clean apps venv (missing `scipy`), which
the test uses as the crash case. With `--no-media` the daemon's idle reset
(sleep after a crash) never runs, so that check is skipped and stays a
hardware check. Becomes CI integration tests under #146 / #131.
