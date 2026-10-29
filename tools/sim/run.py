"""Run the sim checks end to end: sim daemon, controller, test apps, smoke.py.

    uv run python tools/sim/run.py [--work-dir DIR]

Starts its own sim daemon (through ``sim_daemon.py``) and controller on free
ports, installs the test apps from ``tools/sim/apps`` into a fresh apps venv,
waits for both servers, runs ``smoke.py`` against them and tears everything
down. Nothing is shared with a daemon or controller already running, and the
checkout's own ``apps_venv`` is left alone.

The controller binary comes from ``SIM_CONTROLLER_BIN`` (default
``controller/target/release/reachy-controller``; build it first, see
``tools/sim/README.md``). Logs, state and the apps venv go in ``--work-dir``
(default: a new temporary directory, kept and printed when a check fails).
Exit code is smoke.py's: 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tools" / "sim"
APPS = sorted(p for p in (SIM / "apps").iterdir() if (p / "pyproject.toml").exists())
READY_TIMEOUT = 120.0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def install_apps(apps_root: Path) -> None:
    """A fresh apps venv with the test apps, as the daemon's install would make.

    Created here rather than through the daemon's install API, which would
    also pre-install the latest ``reachy-mini`` from PyPI: the test apps
    don't import it.
    """
    venv = apps_root / "apps_venv"
    subprocess.run(["uv", "venv", "--quiet", "--python", sys.executable, str(venv)], check=True)
    subprocess.run(
        ["uv", "pip", "install", "--quiet", "--python", str(venv / "bin" / "python"), *map(str, APPS)],
        check=True,
    )


def wait_up(url: str, proc: subprocess.Popen, name: str) -> None:
    end = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited with code {proc.returncode} before it was ready")
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{name} not ready at {url} after {READY_TIMEOUT:.0f} s")


def stop(proc: subprocess.Popen | None, timeout: float = 30.0) -> None:
    """SIGINT the process group (the daemon stops its app and backend), then kill."""
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    except ProcessLookupError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work-dir", type=Path, help="logs, state and apps venv (default: a temp dir)")
    args = parser.parse_args()

    controller_bin = Path(
        os.getenv("SIM_CONTROLLER_BIN", ROOT / "controller" / "target" / "release" / "reachy-controller")
    )
    if not controller_bin.exists():
        print(f"controller binary not found: {controller_bin} (see tools/sim/README.md)", file=sys.stderr)
        return 2

    temporary = args.work_dir is None
    work = Path(tempfile.mkdtemp(prefix="rb-sim-")) if temporary else args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    daemon_port = free_port()
    while (controller_port := free_port()) == daemon_port:
        pass
    # The app UI proxy too, so a dev controller on the default 7863 can't collide.
    while (app_port := free_port()) in (daemon_port, controller_port):
        pass
    daemon_url = f"http://127.0.0.1:{daemon_port}"
    controller_url = f"http://127.0.0.1:{controller_port}"
    print(f"sim run in {work}: daemon {daemon_url}, controller {controller_url}", flush=True)

    daemon = controller = None
    code = 1
    try:
        install_apps(work / "apps")
        with open(work / "daemon.log", "wb") as log:
            daemon = subprocess.Popen(
                [
                    sys.executable, str(SIM / "sim_daemon.py"),
                    "--sim", "--headless", "--no-media", "--desktop-app-daemon",
                    "--no-autostart", "--no-wake-up-on-start",
                    "--fastapi-port", str(daemon_port),
                ],
                cwd=ROOT,
                env={**os.environ, "REACHY_BRIDGE_STATE_DIR": str(work / "daemon-state"),
                     "REACHY_BRIDGE_APPS_ROOT": str(work / "apps")},
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        wait_up(f"{daemon_url}/api/daemon/status", daemon, "daemon")
        with open(work / "controller.log", "wb") as log:
            controller = subprocess.Popen(
                [str(controller_bin)],
                env={**os.environ, "DAEMON_URL": daemon_url, "CONTROLLER_PORT": str(controller_port),
                     "CONTROLLER_APP_PORT": str(app_port),
                     "REACHY_BRIDGE_STATE_DIR": str(work / "controller-state"),
                     "REACHY_DAEMON_UNIT": "none.service"},
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        wait_up(f"{controller_url}/api/daemon/status", controller, "controller")
        code = subprocess.run(
            [sys.executable, str(SIM / "smoke.py")],
            env={**os.environ, "SIM_DAEMON": daemon_url, "SIM_CONTROLLER": controller_url},
        ).returncode
    except (RuntimeError, subprocess.CalledProcessError) as e:
        print(f"sim run failed: {e}", file=sys.stderr)
    finally:
        stop(controller)
        stop(daemon)
    if code == 0 and temporary:
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(f"logs: {work / 'daemon.log'} {work / 'controller.log'}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
