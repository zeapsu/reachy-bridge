"""Sim smoke test for the controller <-> daemon contract.

Runs the flows that broke during the #145 QA pass against a real daemon in
MuJoCo sim mode (default daemon :8100, controller :7862). Moves only the
simulated robot. Exit code 0 = all checks passed.
"""

from __future__ import annotations

import os
import sys
import time

import httpx

DAEMON = os.getenv("SIM_DAEMON", "http://127.0.0.1:8100")
CTRL = os.getenv("SIM_CONTROLLER", "http://127.0.0.1:7862")
APP = os.getenv("SIM_APP", "reachy_mini_radio")
# An app that exits on its own shortly after starting (hello_world lacks scipy
# in a clean apps_venv), used to exercise the daemon's crash -> sleep path.
CRASH_APP = os.getenv("SIM_CRASH_APP", "hello_world")

d = httpx.Client(base_url=DAEMON, timeout=30)
c = httpx.Client(base_url=CTRL, timeout=60)
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  - ' + detail if detail else ''}", flush=True)


def daemon_state() -> tuple[str, str | None]:
    s = d.get("/api/daemon/status").json()
    return s.get("state"), (s.get("backend_status") or {}).get("motor_control_mode")


def ctrl_status() -> str:
    return c.get("/api/daemon/status").json()["status"]


def wait(pred, timeout: float = 30.0, step: float = 0.5) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def current_app() -> dict | None:
    r = d.get("/api/apps/current-app-status")
    return r.json() if r.content.strip() else None


def app_state() -> str | None:
    cur = current_app()
    return cur.get("state") if cur else None


def stop_any_app() -> None:
    if app_state() in ("starting", "running"):
        c.post(f"/api/apps/stop/{APP}")
        wait(lambda: app_state() not in ("starting", "running", "stopping"), 30)


# --- reset to a known state: robot off ---------------------------------
stop_any_app()
c.post("/api/daemon/stop")
wait(lambda: daemon_state()[0] in ("stopped", "not_initialized"), 40)

# 1. power on wakes the robot (not just the backend)
c.post("/api/daemon/start")
ok = wait(lambda: daemon_state() == ("running", "enabled"), 40)
check("power on -> running with motors enabled", ok, str(daemon_state()))
check("controller reports running", ctrl_status() == "running", ctrl_status())

# 2. power off -> stopped, controller agrees
c.post("/api/daemon/stop")
ok = wait(lambda: daemon_state()[0] == "stopped", 40)
check("power off -> stopped", ok, str(daemon_state()))
check("controller reports stopped", ctrl_status() == "stopped", ctrl_status())

# 3. starting an app with the robot off wakes it first
r = c.post(f"/api/apps/start/{APP}").json()
check("app start accepted while off", r.get("status") == "loading", str(r))
ok = wait(lambda: app_state() == "running", 60)
check("app running", ok, str(app_state()))
check("robot awake under the app", daemon_state() == ("running", "enabled"), str(daemon_state()))

# 4. expressions are refused while the app runs
r = c.post("/api/expressions/play/pollen-robotics/reachy-mini-emotions-library/cheerful1")
check("expression refused while app runs", r.status_code == 409, f"HTTP {r.status_code}")

# 5. requested stop keeps the robot awake (no idle-reset sleep)
c.post(f"/api/apps/stop/{APP}")
wait(lambda: app_state() not in ("starting", "running", "stopping"), 30)
time.sleep(4)  # past the 1.5 s idle-reset debounce
check("robot stays awake after stop", daemon_state() == ("running", "enabled"), str(daemon_state()))
check("controller reports running after stop", ctrl_status() == "running", ctrl_status())

# 6. second start on an awake robot
r = c.post(f"/api/apps/start/{APP}").json()
ok = wait(lambda: app_state() == "running", 60)
check("second start runs", ok, str(r))

# 7. crash -> daemon sleeps the robot; controller reports asleep
stop_any_app()
c.post(f"/api/apps/start/{CRASH_APP}")
crashed = wait(lambda: app_state() == "error", 60)
check("crash app exits on its own", crashed, str(app_state()))
if d.get("/api/daemon/status").json().get("no_media"):
    # The daemon's idle reset needs the media loop; --no-media sims skip it.
    print("SKIP  crash -> sleep (daemon idle reset needs media; hardware check)")
else:
    ok = wait(lambda: daemon_state() == ("running", "disabled"), 30)
    check("crash -> daemon puts robot to sleep", ok, str(daemon_state()))
    check("controller reports asleep", ctrl_status() == "asleep", ctrl_status())
    c.post("/api/daemon/start")
    ok = wait(lambda: daemon_state() == ("running", "enabled"), 30)
    check("power on from asleep -> awake", ok, str(daemon_state()))

# 8. a crashed app doesn't block the next start

# 9. app UI readiness: starting an app with a UI eventually reports ready
r = c.post(f"/api/apps/start/{APP}").json()
port = r.get("ui_port")
ready = port is not None and wait(
    lambda: c.get(f"/api/apps/{APP}/ui").json().get("ready") is True, 60
)
check("app UI reports ready", ready, f"port={port}")

# --- leave the sim off -------------------------------------------------
stop_any_app()
c.post("/api/daemon/stop")
wait(lambda: daemon_state()[0] == "stopped", 40)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
