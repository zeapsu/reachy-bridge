"""Keep the robot awake when the operator stops an app.

Upstream, every app exit frees the robot slot, which schedules an idle reset:
1.5 s later the robot plays the sleep move and goes limp
(``docs/daemon-contract.md``, "Idle reset"). That's right for a crash, but a
requested stop should leave the robot at the home pose the app manager
already returns it to. So the slot's became-free hook is detached while
``stop_current_app`` runs; crashes and remote-session drops still reset.

The return home turns torque on without updating the motor mode, so after an
app that ran limp the daemon reported ``disabled`` on a robot holding its
pose: the dashboard showed it asleep, and power off skipped the sleep move.
After the stop, the mode is set to match the torque. A stop that leaves the
robot limp in the sleep pose has torque off and is left alone.
"""

from __future__ import annotations

import logging
from typing import Any

from reachy_bridge_runtime import upstream

logger = logging.getLogger(__name__)

FEATURE = "keep_awake"


async def _stop_current_app(original: Any, manager: Any, *args: Any, **kwargs: Any) -> Any:
    result = await _stop_keeping_awake(original, manager, *args, **kwargs)
    backend = upstream.daemon_backend(upstream.manager_daemon(manager))
    if backend is not None and upstream.sync_torque_mode(backend):
        logger.info("App stop left torque on; motor mode set to enabled")
    return result


async def _stop_keeping_awake(original: Any, manager: Any, *args: Any, **kwargs: Any) -> Any:
    daemon = upstream.manager_daemon(manager)
    handler = upstream.slot_free_handler(daemon)
    if handler is None or not upstream.set_slot_free_handler(daemon, None):
        return await original(manager, *args, **kwargs)
    try:
        return await original(manager, *args, **kwargs)
    finally:
        backend = upstream.daemon_backend(daemon)
        # daemon.stop() detaches the hook itself; don't re-arm it then.
        if (
            upstream.slot_free_handler(daemon) is None
            and backend is not None
            and not upstream.backend_shutting_down(backend)
        ):
            upstream.set_slot_free_handler(daemon, handler)


def apply() -> None:
    upstream.wrap_stop_current_app(_stop_current_app)
    logger.info("Keep-awake on app stop: on")
