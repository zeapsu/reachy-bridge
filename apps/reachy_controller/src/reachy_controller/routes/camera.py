"""Camera ownership and daemon camera metadata routes."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

from reachy_controller.app_supervisor import AppSupervisor
from reachy_controller.daemon_proxy import DaemonProxy

router = APIRouter(prefix="/api", tags=["camera"])


@router.get("/camera/availability")
async def camera_availability(request: Request) -> dict[str, Any]:
    """Return whether the dashboard may take camera ownership."""
    supervisor: AppSupervisor = request.app.state.app_supervisor
    holder = supervisor.camera_holder()
    if holder is not None:
        return {"available": False, "reason": "in_use", "holder": holder}

    proxy: DaemonProxy = request.app.state.daemon_proxy
    media_status = await proxy.get_media_status()
    daemon_status = None
    if media_status is None:
        daemon_status = await proxy.get_daemon_status()

    if media_status is None and daemon_status is None:
        return {"available": False, "reason": "daemon_down", "holder": None}

    return {
        "available": supervisor.camera_available(),
        "reason": "available",
        "holder": None,
    }


@router.get("/camera/specs")
async def camera_specs(request: Request) -> dict[str, Any]:
    """Return one-shot camera specs from the daemon."""
    proxy: DaemonProxy = request.app.state.daemon_proxy
    try:
        return await proxy.get_json("camera/specs")
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="Daemon unavailable") from None
