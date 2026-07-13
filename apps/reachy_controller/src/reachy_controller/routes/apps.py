"""API routes for local app management."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api", tags=["apps"])


@router.get("/apps/status")
async def get_apps_status(request: Request) -> list[dict[str, Any]]:
    """Get status for all managed apps."""
    supervisor = request.app.state.app_supervisor
    return supervisor.get_all_status()


@router.post("/apps/start/{name}")
async def start_app(name: str, request: Request) -> dict[str, Any]:
    """Start an app. Returns status dict with error if refused."""
    # ponytail: check-then-act across app/expression state is non-atomic; single-user UI
    if request.app.state.expression_player.active:
        return {
            "error": "Robot is playing an expression — wait for it to finish.",
            "name": name,
            "status": "refused",
        }
    supervisor = request.app.state.app_supervisor
    result = await supervisor.start_app(name)

    # Check for error status code
    if result.get("status") == 404:
        raise HTTPException(status_code=404, detail=result.get("error", "Unknown app"))

    return result


@router.post("/apps/stop/{name}")
async def stop_app(name: str, request: Request) -> dict[str, Any]:
    """Stop an app. Returns status dict."""
    supervisor = request.app.state.app_supervisor
    result = await supervisor.stop_app(name)

    # Check for error status code
    if result.get("status") == 404:
        raise HTTPException(status_code=404, detail=result.get("error", "Unknown app"))

    return result
