"""Robot power routes, backed by the daemon's own start/stop API.

systemd owns the daemon process. "Power" here switches the robot backend on
and off inside the always-running daemon: on enables the motors and plays the
wake-up move (ending at the home pose), off stops any app, plays the sleep
move and releases torque.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

from reachy_controller.daemon_apps import APP_CONTROL_TIMEOUT_S
from reachy_controller.daemon_proxy import DaemonProxy

router = APIRouter(prefix="/api/daemon", tags=["daemon"])

# The sleep move takes about 5 s, then the daemon tears the backend down.
POWER_OFF_TIMEOUT_S = 40.0


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
        return state
    backend = status.get("backend_status") or {}
    if backend.get("motor_control_mode") == "disabled":
        return "asleep"
    return "running"


async def _wait_until(check, timeout: float, interval: float = 0.25) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await check():
            return True
        await asyncio.sleep(interval)
    return False


def power_lock(request: Request) -> asyncio.Lock:
    """Lock held by every action that powers, wakes or sleeps the robot."""
    return request.app.state.power_lock


async def ensure_awake(request: Request, *, timeout: float = 20.0) -> None:
    """Make sure the robot is on, motors enabled and the wake-up move finished.

    The daemon does not wake the robot when it starts an app, and it puts the
    robot to sleep when an app stops, so apps started from the controller
    would otherwise run against a limp robot. Call it with ``power_lock`` held.
    """
    proxy: DaemonProxy = request.app.state.daemon_proxy
    status = await proxy.get_daemon_status()
    if status is None:
        return  # unknown: let the daemon decide and report
    state = _robot_state(status)
    motors = (status.get("backend_status") or {}).get("motor_control_mode")
    if state == "asleep":
        await _post(request, "motors/set_mode/enabled", {})
        await _post(request, "move/play/wake_up", {})
    elif state == "running" and motors == "gravity_compensation":
        # Under gravity compensation the motors ignore every move target, so
        # an app would run on a robot that can't move. Return to position
        # control; the head stays where it was guided.
        await _post(request, "motors/set_mode/enabled", {})
    elif state != "running":
        # A power-on already in flight is still "starting": wait for it.
        if state != "starting":
            await _post(request, "daemon/start", {"wake_up": "true"})

        async def running() -> bool:
            current = await proxy.get_daemon_status()
            return current is not None and _robot_state(current) == "running"

        if not await _wait_until(running, timeout):
            raise HTTPException(status_code=504, detail="Robot did not wake up in time")

    async def move_done() -> bool:
        try:
            moves = await proxy.get_json("move/running")
        except (httpx.HTTPError, ValueError):
            return False
        return not moves

    await _wait_until(move_done, timeout)
    request.app.state.expression_player.needs_home = False


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
        # Lets the dashboard tell the sleep move (motors on) from torque release.
        "motors": (status.get("backend_status") or {}).get("motor_control_mode"),
        "error": status.get("error"),
    }


async def _power_on_locked(request: Request, *, wake: bool = True) -> dict[str, Any]:
    """Power on; call with ``power_lock`` held.

    ``wake=False`` starts the backend without the wake move, so the motors stay
    limp: the robot comes back asleep.
    """
    proxy: DaemonProxy = request.app.state.daemon_proxy
    status = await proxy.get_daemon_status()
    state = _robot_state(status) if status is not None else None
    if state in ("running", "starting"):
        return {"status": state}
    if state == "asleep":
        await _post(request, "motors/set_mode/enabled", {})
        result = await _post(request, "move/play/wake_up", {})
    else:
        result = await _post(request, "daemon/start", {"wake_up": "true" if wake else "false"})
    # The wake-up move ends at the home pose, so expressions may run once the
    # daemon reports running (its state gates them until then).
    request.app.state.expression_player.needs_home = not wake
    return result


async def _power_off_locked(request: Request) -> dict[str, Any]:
    """Stop any app, play the sleep move and release torque; call with ``power_lock`` held."""
    proxy: DaemonProxy = request.app.state.daemon_proxy
    if await request.app.state.daemon_apps.refresh_active():
        with contextlib.suppress(httpx.RequestError):
            await proxy.request(
                "POST", "apps/stop-current-app", timeout=APP_CONTROL_TIMEOUT_S
            )
    # Already asleep: skip the sleep move. It re-enables the motors and lifts
    # the head first, so the robot would appear to wake before sleeping.
    status = await proxy.get_daemon_status()
    asleep = status is not None and _robot_state(status) == "asleep"
    result = await _post(
        request, "daemon/stop", {"goto_sleep": "false" if asleep else "true"}
    )
    request.app.state.expression_player.needs_home = True
    return result


@router.post("/start")
async def start_robot(request: Request) -> dict[str, Any]:
    """Switch the robot on: enable motors and play the wake-up move.

    If the backend is already up but asleep, wake it in place instead of
    starting it again.
    """
    async with power_lock(request):
        return await _power_on_locked(request)


@router.post("/stop")
async def stop_robot(request: Request) -> dict[str, Any]:
    """Stop any app, play the sleep move and release torque."""
    async with power_lock(request):
        return await _power_off_locked(request)


async def _kinematics(request: Request) -> dict[str, Any]:
    proxy: DaemonProxy = request.app.state.daemon_proxy
    try:
        response = await proxy.request("GET", "bridge/kinematics")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="Reachy daemon unavailable") from exc
    if response.status_code == 404:
        raise HTTPException(status_code=501, detail="This daemon can't switch kinematics")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Daemon couldn't report kinematics")
    return response.json()


@router.get("/kinematics")
async def get_kinematics(request: Request) -> dict[str, Any]:
    """Kinematics engine for the next power-on, the running one, and what's installed."""
    return await _kinematics(request)


@router.put("/kinematics")
async def set_kinematics(request: Request) -> dict[str, Any]:
    """Switch the kinematics engine.

    The daemon only changes engines while the robot is off, so a robot that is
    on (or asleep) is powered off, switched and powered back on. Refused while
    an app runs, so the switch never stops an app on its own.
    """
    try:
        engine = (await request.json())["engine"]
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail='Body must be {"engine": ...}') from exc
    proxy: DaemonProxy = request.app.state.daemon_proxy
    async with power_lock(request):
        current = await _kinematics(request)
        if engine == current.get("engine") and current.get("active") in (None, engine):
            return {**current, "restarted": False}
        if await request.app.state.daemon_apps.refresh_active():
            raise HTTPException(status_code=409, detail="Stop the app before switching kinematics")
        status = await proxy.get_daemon_status()
        state = _robot_state(status) if status is not None else None
        if state in ("starting", "stopping"):
            raise HTTPException(status_code=409, detail=f"Robot is {state}; try again shortly")
        # A failed start can leave "error" with the backend still up; that is on.
        backend_up = status is not None and status.get("backend_status") is not None
        was_on = state in ("running", "asleep") or (state == "error" and backend_up)
        # Come back as the user had it: asleep stays asleep (no wake move).
        wake = state != "asleep"
        if was_on:
            await _power_off_locked(request)

            async def off() -> bool:
                # The daemon reports stopped (or error) slightly before it drops
                # the backend, and the switch is refused until it has.
                current = await proxy.get_daemon_status()
                return (
                    current is not None
                    and current.get("backend_status") is None
                    and _robot_state(current) in ("stopped", "not_initialized", "error")
                )

            if not await _wait_until(off, POWER_OFF_TIMEOUT_S):
                raise HTTPException(status_code=504, detail="Robot did not power off in time")
        try:
            response = await proxy.request(
                "PUT", "bridge/kinematics", content=json.dumps({"engine": engine}).encode(),
                headers={"content-type": "application/json"},
            )
        except httpx.RequestError as exc:
            if was_on:
                with contextlib.suppress(HTTPException):
                    await _power_on_locked(request, wake=wake)
            raise HTTPException(status_code=503, detail="Reachy daemon unavailable") from exc
        if response.status_code >= 400:
            # Leave the robot as the user had it, on the old engine.
            if was_on:
                await _power_on_locked(request, wake=wake)
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            raise HTTPException(status_code=response.status_code, detail=detail or "Daemon refused")
        if was_on:
            await _power_on_locked(request, wake=wake)
        return {**response.json(), "restarted": was_on}
