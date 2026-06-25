"""Daemon process control routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from reachy_controller.daemon_supervisor import DaemonSupervisor

router = APIRouter(prefix="/api/daemon", tags=["daemon"])


async def _get_daemon_running(request: Request) -> bool:
    """Check if the daemon is reachable via HTTP.

    Reuses the existing DaemonProxy reachability mechanism.
    """
    proxy = request.app.state.daemon_proxy
    try:
        # Use the existing full state endpoint as the reachability check
        state = await proxy.get_full_state()
        return state is not None
    except Exception:
        return False


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
