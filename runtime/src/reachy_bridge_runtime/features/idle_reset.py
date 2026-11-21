"""Tell the controller when the daemon's idle reset is pending (#292).

When the robot slot frees without a requested stop (an app exits on its own,
or a remote session drops), the daemon puts the robot to sleep 1.5 s later
(15 s after a hand-off), which takes 5-8 s (``docs/daemon-contract.md``, "Idle
reset"). Neither ``daemon/stop`` nor ``stop-current-app`` waits for it: they
stop its move part-way, so the head can be left mid-pose or dropped where it
stopped, and on reachy-mini 1.14.0 the stop then fails on the move guard. No
upstream endpoint shows the reset, so ``GET /api/bridge/idle-reset`` answers
``{"pending": true}`` from its grace period until it ends, and the controller
waits for ``false`` before it sends either.

The reset is scheduled with ``call_soon_threadsafe`` on the backend's loop,
in the same synchronous step that marks the app ``done`` or ``error``. The
route reads the task on that loop too, so its read queues behind any
scheduling the controller could have seen and can't miss a reset that is
about to start.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

# Module level: FastAPI resolves the (string) annotations from here.
from fastapi import Request

from reachy_bridge_runtime import upstream

logger = logging.getLogger(__name__)

FEATURE = "idle_reset"
# Answer "pending" rather than guess when the backend loop doesn't answer.
READ_TIMEOUT_S = 1.0


async def pending(backend: Any) -> bool:
    if backend is None:
        return False
    loop = upstream.backend_loop(backend)
    if loop is None or loop.is_closed():
        # No loop, no idle reset (a --no-media daemon never runs one).
        return False

    async def read() -> bool:
        return upstream.idle_reset_running(backend)

    try:
        future = asyncio.run_coroutine_threadsafe(read(), loop)
        return await asyncio.wait_for(asyncio.wrap_future(future), READ_TIMEOUT_S)
    except TimeoutError:
        # The controller polls while it waits: one line per read, no trace.
        logger.warning("Backend loop busy for %.0f s; reporting the idle reset pending", READ_TIMEOUT_S)
        return True
    except Exception:
        logger.warning("Could not read the idle reset state; reporting it pending", exc_info=True)
        return True


def install_route(app: Any) -> None:
    @app.get("/api/bridge/idle-reset", include_in_schema=False)
    async def get_idle_reset(request: Request) -> dict[str, Any]:
        backend = upstream.daemon_backend(upstream.app_daemon(request.app))
        return {"pending": await pending(backend)}


def apply() -> None:
    upstream.on_create_app(install_route)
    logger.info("Idle reset state route: on")
