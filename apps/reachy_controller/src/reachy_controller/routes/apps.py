"""API routes for local app management."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api", tags=["apps"])


@router.get("/apps/status")
async def get_apps_status(request: Request) -> list[dict[str, Any]]:
    """Get status for all managed apps, including native settings support."""
    supervisor = request.app.state.app_supervisor
    statuses = await supervisor.aget_all_status()
    settings = request.app.state.app_settings
    return [{**status, "has_settings": settings.supports(status["name"])} for status in statuses]

@router.get("/apps/favorites")
async def get_app_favorites(request: Request) -> dict[str, list[str]]:
    """Return the persisted app favorites for this robot."""
    supervisor = request.app.state.app_supervisor
    return {"favorites": supervisor.get_favorites()}


def _validate_favorites_payload(payload: object, known_apps: set[str]) -> list[str]:
    """Validate the small favorites contract and identify its bad entry."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Invalid favorites payload")
    favorites = payload.get("favorites")
    if not isinstance(favorites, list):
        raise HTTPException(status_code=422, detail=f"Invalid favorite entry: {favorites!r}")

    seen: set[str] = set()
    for entry in favorites:
        if not isinstance(entry, str):
            raise HTTPException(status_code=422, detail=f"Invalid favorite entry: {entry!r}")
        if entry not in known_apps:
            raise HTTPException(status_code=422, detail=f"Unknown favorite app: {entry}")
        if entry in seen:
            raise HTTPException(status_code=422, detail=f"Duplicate favorite app: {entry}")
        seen.add(entry)
    if len(favorites) > 3:
        raise HTTPException(
            status_code=422,
            detail=f"Too many favorites (maximum 3); offending entry: {favorites[3]}",
        )
    return favorites


@router.put("/apps/favorites")
async def put_app_favorites(request: Request) -> dict[str, list[str]]:
    """Validate and atomically persist app favorites."""
    try:
        payload = await request.json()
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid favorites payload") from None
    supervisor = request.app.state.app_supervisor
    favorites = _validate_favorites_payload(payload, set(supervisor.APPS))
    return {"favorites": supervisor.set_favorites(favorites)}


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
