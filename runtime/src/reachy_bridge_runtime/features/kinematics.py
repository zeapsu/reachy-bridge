"""Switchable kinematics engine that survives restarts (#150).

- ``GET``/``PUT /api/bridge/kinematics`` reads and sets the engine the next
  power-on uses. Switching under a running backend would need a restart,
  which moves the robot, so it is refused; the controller sequences power
  off, switch, power on itself.
- The choice is saved in the bridge state dir and applied at startup.
- ``Daemon.restart`` calls ``start`` without the engine, which would fall back
  to the default, so the engine of the last explicit start is reused.

Gravity compensation needs Placo, which is optional (it links system
libraries that may be missing), so it's offered only when it imports.
"""

from __future__ import annotations

import functools
import json
import logging
import sys
from pathlib import Path
from typing import Any

# Module level: FastAPI resolves the (string) annotations from here.
from fastapi import HTTPException, Request

from reachy_bridge_runtime import upstream
from reachy_bridge_runtime.paths import state_dir

logger = logging.getLogger(__name__)

FEATURE = "kinematics"
ENGINES = upstream.KINEMATICS_ENGINES
DEFAULT = upstream.DEFAULT_KINEMATICS


def kinematics_file() -> Path:
    return state_dir() / "kinematics.json"


@functools.cache
def placo_available() -> bool:
    # Import, not just find: the wheel links system libraries (urdfdom) that
    # can be missing even when the package is installed.
    try:
        import placo  # noqa: F401
    except Exception:  # ImportError, or OSError from a missing shared library
        return False
    return True


def available() -> list[str]:
    return [engine for engine in ENGINES if engine != "Placo" or placo_available()]


def saved_engine() -> str:
    try:
        engine = json.loads(kinematics_file().read_text())["engine"]
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT
    return engine if engine in ENGINES else DEFAULT


def apply_saved_engine(argv: list[str]) -> None:
    """Start with the last chosen engine unless the command line names one."""
    if any(arg.startswith(upstream.KINEMATICS_FLAG) for arg in argv[1:]):
        return
    engine = saved_engine()
    if engine == "Placo" and not placo_available():
        logger.warning("Saved kinematics engine Placo is not installed; using %s", DEFAULT)
        return
    if engine != DEFAULT:
        argv += [upstream.KINEMATICS_FLAG, engine]
        logger.info("Using saved kinematics engine %s", engine)


async def _start(original: Any, daemon: Any, *args: Any, **kwargs: Any) -> Any:
    engine = upstream.start_kinematics(kwargs)
    if engine is not None:
        daemon._bridge_kinematics = engine
    elif getattr(daemon, "_bridge_kinematics", None) is not None:
        kwargs = upstream.with_kinematics(kwargs, daemon._bridge_kinematics)
    return await original(daemon, *args, **kwargs)


def _payload(app: Any) -> dict[str, Any]:
    backend = upstream.daemon_backend(upstream.app_daemon(app))
    return {
        "engine": upstream.app_kinematics(app),
        "active": upstream.backend_kinematics(backend) if backend is not None else None,
        "available": available(),
    }


def install_route(app: Any) -> None:
    @app.get("/api/bridge/kinematics", include_in_schema=False)
    async def get_kinematics(request: Request) -> dict[str, Any]:
        return _payload(request.app)

    @app.put("/api/bridge/kinematics", include_in_schema=False)
    async def set_kinematics(request: Request) -> dict[str, Any]:
        try:
            engine = (await request.json())["engine"]
        except (ValueError, KeyError, TypeError):
            raise HTTPException(status_code=422, detail='Body must be {"engine": ...}') from None
        if engine not in ENGINES:
            raise HTTPException(status_code=400, detail=f"Unknown kinematics engine: {engine}")
        if engine == "Placo" and not placo_available():
            raise HTTPException(status_code=400, detail="Placo kinematics is not installed")
        if upstream.daemon_backend(upstream.app_daemon(request.app)) is not None:
            raise HTTPException(status_code=409, detail="Turn the robot off to change kinematics")
        upstream.set_app_kinematics(request.app, engine)
        path = kinematics_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"engine": engine}))
        tmp.replace(path)
        logger.info("Kinematics engine for the next power-on: %s", engine)
        return _payload(request.app)


def apply(argv: list[str] = sys.argv) -> None:
    apply_saved_engine(argv)
    upstream.wrap_daemon_start(_start)
    upstream.on_create_app(install_route)
    # Import placo now rather than on the first request, where the import
    # would block the daemon's event loop.
    placo_available()
    logger.info("Kinematics switch: on (available: %s)", ", ".join(available()))
