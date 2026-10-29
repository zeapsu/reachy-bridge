"""Run apps against the sim daemon (#218).

A sim daemon (sim mode, #149) runs on the robot's host beside the robot's
daemon. Apps reach their daemon through the SDK, which connects to
``localhost:8000`` unless told otherwise: an app the sim daemon started would
drive the real robot. So in a sim daemon started with ``--desktop-app-daemon``:

- apps live in their own venv under ``REACHY_BRIDGE_APPS_ROOT`` (the sim's
  working directory), never in the robot's ``apps_venv``;
- at startup and before each app start, a shim is written into that venv
  (``sim_apps_shim.py``) that sends every loopback connection on the robot
  daemon's port to the sim daemon's port, so apps written for the robot drive
  the sim. An app is refused, not started, if the shim can't be written or
  its interpreter isn't in the sim's venv.

The shim covers an app's ordinary daemon connections, not hostile code (the
app runs as the same user as the robot's daemon). What keeps a missed
connection off the robot is the controller: it stops the robot's daemon for
the whole of sim mode (controller/src/sim.rs), so nothing listens on its
port.

The robot daemon's port is ``REACHY_BRIDGE_ROBOT_DAEMON_PORT`` (8000). The
sim daemon reports no media, so apps get no camera or audio in sim mode.

This feature fails closed (main.FATAL_FEATURES): a sim daemon with apps
whose seam doesn't match refuses to start.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from reachy_bridge_runtime import upstream
from reachy_bridge_runtime.features import app_bind

logger = logging.getLogger(__name__)

FEATURE = "sim_apps"

SHIM = Path(__file__).with_name("sim_apps_shim.py")
MODULE = "reachy_bridge_sim_port"
# Sorts before other .pth files, so it is in place before their code runs.
PTH = f"00_{MODULE}.pth"


def _flag(args: list[str], name: str) -> str | None:
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(f"{name}="):
            return arg.split("=", 1)[1]
    return None


def ports(args: list[str]) -> tuple[int, int]:
    """(robot daemon port, this sim daemon's port); refuses equal ports."""
    robot = int(os.getenv("REACHY_BRIDGE_ROBOT_DAEMON_PORT") or 8000)
    sim = int(_flag(args, "--fastapi-port") or 8000)
    if sim == robot:
        raise RuntimeError(f"a sim daemon with apps must not use the robot daemon's port {robot}")
    return robot, sim


def apps_root() -> Path:
    """Where the sim keeps its apps venv; never the robot's."""
    root = os.getenv("REACHY_BRIDGE_APPS_ROOT")
    if not root or not Path(root).is_absolute():
        raise RuntimeError("a sim daemon with apps needs REACHY_BRIDGE_APPS_ROOT, an absolute path")
    root_path = Path(root).resolve()
    if root_path == upstream.default_apps_root().resolve():
        raise RuntimeError(f"REACHY_BRIDGE_APPS_ROOT={root} is the robot's apps folder")
    return root_path


def shim_source(robot_port: int, sim_port: int) -> str:
    source = SHIM.read_text(encoding="utf-8")
    out = source.replace("ROBOT_PORT = 8000\n", f"ROBOT_PORT = {robot_port}\n", 1)
    out = out.replace("SIM_PORT = 8001\n", f"SIM_PORT = {sim_port}\n", 1)
    if f"ROBOT_PORT = {robot_port}\n" not in out or f"SIM_PORT = {sim_port}\n" not in out:
        raise RuntimeError("the sim port shim's constants changed; not writing it")
    return out


def install_shim(python: Path, robot_port: int, sim_port: int) -> Path:
    target = app_bind.site_packages(python)
    app_bind._write(target / f"{MODULE}.py", shim_source(robot_port, sim_port))
    app_bind._write(target / PTH, f"import {MODULE}\n")
    return target


def apply(args: list[str]) -> None:
    robot_port, sim_port = ports(args)
    root = apps_root()
    upstream.set_apps_root(root)
    existing = root / "apps_venv" / "bin" / "python"
    if existing.exists():
        install_shim(existing, robot_port, sim_port)
    logger.info("sim apps in %s, sent to the sim daemon on :%d instead of :%d", root, sim_port, robot_port)

    async def start_app(original: Any, manager: Any, app_name: str, *a: Any, **kw: Any) -> Any:
        python = Path(upstream.app_python(manager, app_name))
        if not python.absolute().is_relative_to(root):
            raise RuntimeError(f"Not starting {app_name}: its interpreter {python} isn't in the sim's apps folder")
        try:
            install_shim(python, robot_port, sim_port)
        except Exception as exc:
            logger.exception("Not starting %s: could not point it at the sim daemon", app_name)
            raise RuntimeError(f"Not starting {app_name}: could not point it at the sim daemon ({exc})") from exc
        return await original(manager, app_name, *a, **kw)

    upstream.wrap_start_app(start_app)
