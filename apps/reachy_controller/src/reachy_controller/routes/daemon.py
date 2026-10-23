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


def _robot_state(status: dict[str, Any]) -> str:
    """Daemon state, refined by motor mode.

    The daemon stays ``running`` after it puts the robot to sleep on its own
    (for example the idle reset after an app exits), so a running backend with
    motors disabled is reported as ``asleep`` rather than on.
    """
    state = str(status.get("state", "unknown"))
    if state != "running":
        return _TRANSITIONS.get(state, state)
    backend = status.get("backend_status") or {}
    if backend.get("motor_control_mode") == "disabled":
        return "asleep"
    return "running"


@router.get("/status")
async def get_daemon_status(request: Request) -> dict[str, Any]:
    """Robot state, in the shape the dashboard power button expects."""
    proxy: DaemonProxy = request.app.state.daemon_proxy
    status = await proxy.get_daemon_status()
    if status is None:
        return {"reachable": False, "running": False, "manageable": False, "status": "offline"}
    state = _robot_state(status)
    return {
        "reachable": True,
        "running": state == "running",
        "manageable": True,
        "status": state,
        "error": status.get("error"),
    }


@router.post("/start")
async def start_robot(request: Request) -> dict[str, Any]:
    """Switch the robot on: enable motors and play the wake-up move.

    If the backend is already up but asleep, wake it in place instead of
    starting it again.
    """
    proxy: DaemonProxy = request.app.state.daemon_proxy
    status = await proxy.get_daemon_status()
    if status is not None and _robot_state(status) == "asleep":
        await _post(request, "motors/set_mode/enabled", {})
        result = await _post(request, "move/play/wake_up", {})
    else:
        result = await _post(request, "daemon/start", {"wake_up": "true"})
    # The wake-up move ends at the home pose, so expressions may run once the
    # daemon reports running (its state gates them until then).
    request.app.state.expression_player.needs_home = False
    return result


@router.post("/stop")
async def stop_robot(request: Request) -> dict[str, Any]:
    """Stop any app, play the sleep move and release torque."""
    proxy: DaemonProxy = request.app.state.daemon_proxy
    if await request.app.state.daemon_apps.refresh_active():
        with contextlib.suppress(httpx.RequestError):
            await proxy.request("POST", "apps/stop-current-app")
    # Already asleep: skip the sleep move. It re-enables the motors and lifts
    # the head first, so the robot would appear to wake before sleeping.
    status = await proxy.get_daemon_status()
    asleep = status is not None and _robot_state(status) == "asleep"
    result = await _post(request, "daemon/stop", {"goto_sleep": "false" if asleep else "true"})
    request.app.state.expression_player.needs_home = True
    return result
