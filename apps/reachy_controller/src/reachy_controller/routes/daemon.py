"""Daemon process control routes."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request

from reachy_controller.daemon_supervisor import DaemonSupervisor

router = APIRouter(prefix="/api/daemon", tags=["daemon"])

# TTL for the daemon status cache, in seconds. A ~1s stale window is acceptable:
# daemon up/down transitions are infrequent and the client polls status at ~1 Hz,
# so this coalesces redundant full-state probes without status feeling sticky.
_DAEMON_STATUS_TTL_SECONDS = 1.0


async def _get_daemon_running(request: Request) -> bool:
    """Check if the daemon is reachable via HTTP with caching.

    Reuses the existing DaemonProxy reachability mechanism, but caches
    the result for _DAEMON_STATUS_TTL_SECONDS to reduce redundant probes
    from rapid polling.
    """
    proxy = request.app.state.daemon_proxy
    now = time.monotonic()

    # Lazily initialize or retrieve the cache from app state
    cache = getattr(request.app.state, "_daemon_status_cache", None)

    # Check if cache is fresh
    if cache is not None:
        timestamp, cached_result = cache
        if now - timestamp < _DAEMON_STATUS_TTL_SECONDS:
            # Cache is still fresh, return the cached result
            return cached_result

    # Cache is stale or missing, re-probe
    try:
        state = await proxy.get_full_state()
        result = state is not None
    except Exception:
        result = False

    # Update cache with new timestamp and result
    request.app.state._daemon_status_cache = (now, result)

    return result


@router.get("/status")
async def get_daemon_status(request: Request) -> dict[str, Any]:
    """Get daemon status (running, manageable, transient status, pid, start_time).

    Authoritative 'running' comes from daemon's :8000 reachability.
    """
    supervisor: DaemonSupervisor = request.app.state.daemon_supervisor
    running = await _get_daemon_running(request)
    return supervisor.status(running=running)


@router.post("/start")
async def start_daemon(request: Request) -> dict[str, Any]:
    """Start the daemon process."""
    supervisor: DaemonSupervisor = request.app.state.daemon_supervisor
    return await supervisor.start()


@router.post("/stop")
async def stop_daemon(request: Request) -> dict[str, Any]:
    """Stop the daemon process."""
    supervisor: DaemonSupervisor = request.app.state.daemon_supervisor
    return await supervisor.stop()
