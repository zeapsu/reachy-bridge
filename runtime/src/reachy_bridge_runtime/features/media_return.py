"""Give the camera and microphone back to the daemon when an app exits.

An app that wants the hardware for itself (``ReachyMini(media_backend=
"no_media")``, or ``release_media()``) makes the daemon stop its media
server: camera, microphone, WebRTC and the bridge's preview and mic levels
all go dark while it runs. Upstream takes them back only from the app's own
``ReachyMini.__exit__``. An app that crashes, is killed by a stop, or never
uses the context manager leaves the media released until the daemon
restarts: the dashboard shows no video and the mic as unavailable.

Upstream keeps the release across a power off and on too (``Daemon.start``
skips the media while released), so only a service restart cleared it.

So when an app's process ends, however it ends, and no other app has started
since, the bridge takes the media back: a running daemon re-acquires it now;
a stopped one forgets the release, so its next start brings the media up as
usual.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from reachy_bridge_runtime import upstream

logger = logging.getLogger(__name__)

FEATURE = "media_return"

# Keep the follow-up tasks referenced until they finish.
_pending: set[asyncio.Task[None]] = set()

# A start or stop in progress decides on the release by itself part way
# through, so wait for it to settle (up to this long) before acting.
SETTLE_TIMEOUT = 60.0
SETTLE_POLL = 0.5


async def _start_app(original: Any, manager: Any, app_name: str, *args: Any, **kwargs: Any) -> Any:
    daemon = upstream.manager_daemon(manager)
    # A release from before the app (an operator, a tool, a network SDK
    # client) isn't the app's to give back.
    released_before = daemon is not None and upstream.media_released(daemon)
    status = await original(manager, app_name, *args, **kwargs)
    monitor = upstream.app_monitor_task(manager)
    if monitor is not None and not released_before:
        monitor.add_done_callback(lambda _done: _schedule(manager, monitor, app_name))
    return status


def _schedule(manager: Any, monitor: Any, app_name: str) -> None:
    task = asyncio.get_running_loop().create_task(take_back(manager, monitor, app_name))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _settled_state(daemon: Any, timeout: float) -> str | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        state = upstream.daemon_state(daemon)
        if state not in ("starting", "stopping") or asyncio.get_running_loop().time() >= deadline:
            return state
        await asyncio.sleep(SETTLE_POLL)


async def take_back(manager: Any, monitor: Any, app_name: str, settle_timeout: float = SETTLE_TIMEOUT) -> bool:
    """Re-acquire the media ``app_name`` left released; True if it did."""
    daemon = upstream.manager_daemon(manager)
    if daemon is None or not upstream.media_released(daemon):
        return False
    state = await _settled_state(daemon, settle_timeout)
    current = upstream.app_monitor_task(manager)
    if current is not None and current is not monitor and not current.done():
        # A newer app runs; the media is its to release or keep.
        return False
    if not upstream.media_released(daemon):
        return False
    try:
        if state == "running":
            await upstream.acquire_media(daemon)
            logger.info("%s left the camera and microphone released; the daemon has them again", app_name)
            return True
        if state in ("stopped", "not_initialized"):
            upstream.forget_media_release(daemon)
            logger.info("%s left the camera and microphone released; the daemon gets them back when it starts", app_name)
            return True
    except Exception:
        logger.exception("%s left the camera and microphone released; could not take them back", app_name)
        return False
    logger.warning("%s left the camera and microphone released; the daemon is %s, so they stay released", app_name, state)
    return False


def apply() -> None:
    upstream.wrap_start_app(_start_app)
    logger.info("Media back to the daemon after each app: on")
