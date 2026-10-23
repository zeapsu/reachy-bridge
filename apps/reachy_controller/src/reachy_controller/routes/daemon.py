"""Robot power routes, backed by the daemon's own start/stop API.

systemd owns the daemon process. "Power" here switches the robot backend on
and off inside the always-running daemon: on enables the motors and plays the
wake-up move (ending at the home pose), off stops any app, plays the sleep
move and releases torque.
"""

from __future__ import annotations

import contextlib
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

from reachy_controller.daemon_proxy import DaemonProxy

router = APIRouter(prefix="/api/daemon", tags=["daemon"])

_TRANSITIONS = {"starting": "starting", "stopping": "stopping"}


async def _post(request: Request, path: str, params: dict[str, str]) -> dict[str, Any]:
    proxy: DaemonProxy = request.app.state.daemon_proxy
    try:
        response = await proxy.request("POST", path, params=params)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="Reachy daemon unavailable") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        raise HTTPException(status_code=response.status_code, detail=detail or "Daemon refused")
    return response.json()


@router.get("/status")
async def get_daemon_status(request: Request) -> dict[str, Any]:
    """Robot backend state, in the shape the dashboard power button expects."""
    proxy: DaemonProxy = request.app.state.daemon_proxy
    status = await proxy.get_daemon_status()
    if status is None:
        return {"reachable": False, "running": False, "manageable": False, "status": "offline"}
    state = str(status.get("state", "unknown"))
    return {
        "reachable": True,
        "running": state == "running",
        "manageable": True,
        "status": _TRANSITIONS.get(state, state),
        "error": status.get("error"),
    }


@router.post("/start")
async def start_robot(request: Request) -> dict[str, Any]:
    """Switch the robot on: enable motors and play the wake-up move."""
    result = await _post(request, "daemon/start", {"wake_up": "true"})
    # The wake-up move ends at the home pose, so expressions may run once the
    # daemon reports running (its state gates them until then).
    request.app.state.expression_player.needs_home = False
    return result


@router.post("/stop")
async def stop_robot(request: Request) -> dict[str, Any]:
    """Stop any app, play the sleep move and release torque."""
    apps = request.app.state.daemon_apps
    if await apps.refresh_active():
        proxy: DaemonProxy = request.app.state.daemon_proxy
        with contextlib.suppress(httpx.RequestError):
            await proxy.request("POST", "apps/stop-current-app")
    result = await _post(request, "daemon/stop", {"goto_sleep": "true"})
    request.app.state.expression_player.needs_home = True
    return result
