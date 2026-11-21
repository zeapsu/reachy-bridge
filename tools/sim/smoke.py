"""Sim checks for the controller <-> daemon contract.

Runs the flows that broke during the #145 QA pass against a real daemon in
MuJoCo sim mode, through the controller. Checks tagged ``[bug N]`` cover the
four bugs from #146, where the controller assumed daemon behaviour that isn't
true (``docs/daemon-contract.md``, "Rules for the controller"):

1. ``daemon/start?wake_up=false`` doesn't enable the motors: robot shown on
   while limp.
2. The daemon puts the robot to sleep about 1.5 s after an app exits and
   stays ``running``: the UI kept showing "on".
3. Starting an app doesn't wake the robot: the app ran on a limp robot.
4. An app's web server isn't up when its process starts: "site can't be
   reached".

Bugs 1 to 3 only show on a daemon started through ``sim_daemon.py``, which
gives the sim the real robot's motor modes and idle reset. ``run.py`` starts
one, a controller and the test apps, then runs this file. To run it against
servers you started yourself, set ``SIM_DAEMON`` and ``SIM_CONTROLLER``
(defaults :8100 and :7862). Moves only the simulated robot. Exit code 0 = all
checks passed.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time

import httpx

DAEMON = os.getenv("SIM_DAEMON", "http://127.0.0.1:8100")
CTRL = os.getenv("SIM_CONTROLLER", "http://127.0.0.1:7862")
# A long-running app with a web UI that comes up a few seconds late.
APP = os.getenv("SIM_APP", "sim_ui_app")
# An app with no UI that exits with an error shortly after starting.
CRASH_APP = os.getenv("SIM_CRASH_APP", "sim_crash_app")

d = httpx.Client(base_url=DAEMON, timeout=30)
c = httpx.Client(base_url=CTRL, timeout=60)
results: list[tuple[str, bool, str]] = []
AWAKE = ("running", "enabled")
ASLEEP = ("running", "disabled")


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
    if app_state() in ("starting", "running", "error"):
        c.post(f"/api/apps/stop/{APP}")
        wait(lambda: app_state() not in ("starting", "running", "stopping", "error"), 30)


def ui(app: str) -> dict:
    return c.get(f"/api/apps/{app}/ui").json()


def answers(port: int) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def lan_address() -> str | None:
    """This host's address on its default route, if it has one."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # no packet is sent
        except OSError:
            return None
        address = s.getsockname()[0]
    return None if address.startswith("127.") or address == "0.0.0.0" else address


def answers_on(address: str, port: int) -> bool:
    try:
        with socket.create_connection((address, port), timeout=2):
            return True
    except OSError:
        return False


def crash_to_sleep() -> None:
    """Start the crash app; the daemon should put the robot to sleep."""
    stop_any_app()
    c.post(f"/api/apps/start/{CRASH_APP}")
    crashed = wait(lambda: app_state() == "error", 60)
    check("crash app exits on its own", crashed, str(app_state()))
    ok = wait(lambda: daemon_state() == ASLEEP, 30)
    check("[bug 2] crash -> daemon puts robot to sleep, stays running", ok, str(daemon_state()))
    check("[bug 2] controller reports asleep after crash", ctrl_status() == "asleep", ctrl_status())


def idle_reset_pending() -> bool | None:
    r = d.get("/api/bridge/idle-reset")
    return r.json().get("pending") if r.status_code == 200 else None


class Sampler:
    """Every 100 ms while an action runs: the daemon's (state, motor mode),
    the current app's state, then whether the idle reset is pending. Read in
    that order, a sample that shows the controller's action already done can
    only read pending if the controller acted during the reset."""

    def __init__(self) -> None:
        self.samples: list[tuple[tuple[str, str | None], str | None, bool | None]] = []
        self.errors: list[str] = []
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        with httpx.Client(base_url=DAEMON, timeout=5) as sampler:
            self._sample(sampler)

    def _sample(self, sampler: httpx.Client) -> None:
        while not self._done.is_set():
            try:
                s = sampler.get("/api/daemon/status").json()
                daemon = (s.get("state"), (s.get("backend_status") or {}).get("motor_control_mode"))
                r = sampler.get("/api/apps/current-app-status")
                app = (r.json() or {}).get("state") if r.content.strip() else None
                r = sampler.get("/api/bridge/idle-reset")
                pending = r.json().get("pending") if r.status_code == 200 else None
                self.samples.append((daemon, app, pending))
            except Exception as error:  # noqa: BLE001 - any odd body skips one sample
                # A non-JSON body (a teardown error page) skips one sample;
                # it mustn't end the thread and quietly weaken the check.
                self.errors.append(repr(error))
            time.sleep(0.1)

    def __enter__(self) -> Sampler:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._done.set()
        self._thread.join(5)

    def saw_pending(self) -> bool:
        return any(pending for _, _, pending in self.samples)

    def acted_during_reset(self, acted) -> bool:
        """Some sample shows ``acted(daemon, app)`` while the reset was pending."""
        return any(pending and acted(daemon, app) for daemon, app, pending in self.samples)

    def changes(self) -> str:
        """The samples with repeats dropped."""
        out = [f"{d[0]}/{d[1]} app={a} pending={p}" for d, a, p in self.samples]
        changes = " -> ".join(x for i, x in enumerate(out) if i == 0 or out[i - 1] != x)
        return changes + (f" (skipped samples: {self.errors})" if self.errors else "")


def crash_and_catch_idle_reset() -> bool:
    """Start the crash app and return once it has exited, with the daemon's
    idle reset under way (#292)."""
    stop_any_app()
    c.post(f"/api/apps/start/{CRASH_APP}")
    crashed = wait(lambda: app_state() == "error", 60, step=0.1)
    pending = idle_reset_pending()
    check("[#292] idle reset reported pending right after a crash", crashed and pending is True, f"pending={pending}")
    return crashed and pending is True


started = time.monotonic()

# --- reset to a known state: robot off ---------------------------------
stop_any_app()
c.post("/api/daemon/stop")
wait(lambda: daemon_state()[0] in ("stopped", "not_initialized"), 40)

# 1. power on wakes the robot, not just the backend. A start without
# wake-up leaves the motors limp, which the controller must not call "on".
c.post("/api/daemon/start")
ok = wait(lambda: daemon_state() == AWAKE, 40)
check("[bug 1] power on -> running with motors enabled", ok, str(daemon_state()))
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
check("[bug 3] app started while off runs on an awake robot", daemon_state() == AWAKE, str(daemon_state()))

# 4. expressions are refused while the app runs
r = c.post("/api/expressions/play/pollen-robotics/reachy-mini-emotions-library/cheerful1")
check("expression refused while app runs", r.status_code == 409, f"HTTP {r.status_code}")

# 5. requested stop keeps the robot awake (no idle-reset sleep)
c.post(f"/api/apps/stop/{APP}")
wait(lambda: app_state() not in ("starting", "running", "stopping"), 30)
time.sleep(4)  # past the 1.5 s idle-reset debounce
check("robot stays awake after stop", daemon_state() == AWAKE, str(daemon_state()))
check("controller reports running after stop", ctrl_status() == "running", ctrl_status())

# 6. second start on an awake robot
r = c.post(f"/api/apps/start/{APP}").json()
ok = wait(lambda: app_state() == "running", 60)
check("second start runs", ok, str(r))

# 7. crash -> the daemon sleeps the robot and stays running; the controller
# must report asleep, not on. The app has no UI.
crash_to_sleep()
check("app without a UI has no UI port", ui(CRASH_APP).get("port") is None, str(ui(CRASH_APP)))

# 8. starting an app on the asleep robot wakes it first. The crashed app is
# still in the daemon's slot, so this start also covers clearing it.
r = c.post(f"/api/apps/start/{APP}").json()
port = r.get("ui_port")
# 9. the app's UI server comes up a few seconds after the process: the
# controller must not report it ready until it answers.
early = ui(APP)
check(
    "[bug 4] app UI not ready while its server is still down",
    early.get("ready") is False and port is not None and not answers(port),
    f"{early}",
)
ok = wait(lambda: app_state() == "running", 60)
check("app running after crash", ok, str(app_state()))
check("[bug 3] app started while asleep runs on an awake robot", daemon_state() == AWAKE, str(daemon_state()))
ready = port is not None and wait(lambda: ui(APP).get("ready") is True, 60)
check("[bug 4] app UI reports ready", ready, f"port={port}")
check("[bug 4] app UI answers once ready", port is not None and answers(port), f"port={port}")
lan = lan_address()
check(
    "[#186] app UI asked for 0.0.0.0 but listens on loopback only",
    port is not None and (lan is None or not answers_on(lan, port)),
    f"port={port} lan={lan}",
)

# 10. power on from asleep wakes the robot in place
crash_to_sleep()
c.post("/api/daemon/start")
ok = wait(lambda: daemon_state() == AWAKE, 30)
check("[bug 1] power on from asleep -> motors enabled", ok, str(daemon_state()))
check("controller reports running after wake", ctrl_status() == "running", ctrl_status())

# 11. [#292] power off while the daemon puts the robot to sleep after a
# crash: the controller waits for the sleep move, then powers off from asleep,
# instead of cutting the move short (1.14.0: daemon "error", head mid-pose).
if crash_and_catch_idle_reset():
    with Sampler() as s:
        c.post("/api/daemon/stop")
        wait(lambda: daemon_state()[0] in ("stopped", "error"), 40)
    check(
        "[#292] power off waits for the idle reset: no stop while it runs",
        s.saw_pending() and not s.acted_during_reset(lambda daemon, _app: daemon[0] != "running"),
        s.changes(),
    )
    check("[#292] power off after the idle reset ends stopped", daemon_state()[0] == "stopped", str(daemon_state()))

# 12. [#292] starting an app while the daemon puts the robot to sleep after a
# crash: the controller lets the sleep move finish before it clears the crashed
# app and wakes the robot (1.14.0: torque cut wherever the head stopped).
if crash_and_catch_idle_reset():
    with Sampler() as s:
        r = c.post(f"/api/apps/start/{APP}").json()
        ok = wait(lambda: app_state() == "running", 60)
    check("[#292] app start after a crash runs", ok and r.get("status") == "loading", str(r))
    check(
        "[#292] app start waits for the idle reset: crashed app kept until it ends",
        s.saw_pending() and not s.acted_during_reset(lambda _daemon, app: app != "error"),
        s.changes(),
    )
    check("[#292] app start after the idle reset runs on an awake robot", daemon_state() == AWAKE, str(daemon_state()))

# --- leave the sim off -------------------------------------------------
stop_any_app()
c.post("/api/daemon/stop")
wait(lambda: daemon_state()[0] == "stopped", 40)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed in {time.monotonic() - started:.0f} s")
sys.exit(1 if failed else 0)
