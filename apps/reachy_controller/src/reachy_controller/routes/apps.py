"""App routes over the daemon's Hugging Face app manager.

Apps are installed and run by the daemon (one at a time). These routes list
installed apps, start/stop them with the controller's arbitration guard, and
persist up to three favorites for the dashboard panel.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

from reachy_controller.daemon_apps import APP_CONTROL_TIMEOUT_S, DaemonApps
from reachy_controller.daemon_proxy import DaemonProxy
from reachy_controller.paths import state_dir
from reachy_controller.routes.daemon import ensure_awake

router = APIRouter(prefix="/api", tags=["apps"])

MAX_FAVORITES = 3


def _apps(request: Request) -> DaemonApps:
    return request.app.state.daemon_apps


def _favorites_path() -> Path:
    return state_dir() / "favorites.json"


def _load_favorites() -> list[str] | None:
    try:
        payload = json.loads(_favorites_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    favorites = payload.get("favorites") if isinstance(payload, dict) else None
    if isinstance(favorites, list) and all(isinstance(item, str) for item in favorites):
        return favorites[:MAX_FAVORITES]
    return None


def _save_favorites(favorites: list[str]) -> None:
    path = _favorites_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=".favorites.", delete=False
    ) as handle:
        json.dump({"favorites": favorites}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(handle.name, path)


async def _installed_names(request: Request) -> list[str]:
    try:
        return [str(item.get("name")) for item in await _apps(request).installed()]
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Reachy daemon unavailable") from exc


@router.get("/apps/status")
async def get_apps_status(request: Request) -> list[dict[str, Any]]:
    """Installed apps with the daemon's current run state."""
    try:
        return await _apps(request).statuses()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Reachy daemon unavailable") from exc


@router.get("/apps/favorites")
async def get_app_favorites(request: Request) -> dict[str, list[str]]:
    installed = await _installed_names(request)
    saved = _load_favorites()
    favorites = saved if saved is not None else installed[:MAX_FAVORITES]
    return {"favorites": [name for name in favorites if name in installed]}


@router.put("/apps/favorites")
async def put_app_favorites(request: Request) -> dict[str, list[str]]:
    try:
        payload = await request.json()
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid favorites payload") from None
    favorites = payload.get("favorites") if isinstance(payload, dict) else None
    if not isinstance(favorites, list) or not all(isinstance(item, str) for item in favorites):
        raise HTTPException(status_code=422, detail="Invalid favorites payload")
    if len(favorites) != len(set(favorites)):
        raise HTTPException(status_code=422, detail="Duplicate favorite app")
    if len(favorites) > MAX_FAVORITES:
        raise HTTPException(status_code=422, detail="Too many favorites (maximum 3)")
    installed = set(await _installed_names(request))
    unknown = [name for name in favorites if name not in installed]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown favorite app: {unknown[0]}")
    _save_favorites(favorites)
    return {"favorites": favorites}


@router.post("/apps/start/{name}")
async def start_app(name: str, request: Request) -> dict[str, Any]:
    """Start an installed app through the daemon; refused while an expression plays."""
    # ponytail: check-then-act across app/expression state is non-atomic; single-user UI
    if request.app.state.expression_player.active:
        return {
            "error": "Robot is playing an expression — wait for it to finish.",
            "name": name,
            "status": "refused",
        }
    if name not in await _installed_names(request):
        raise HTTPException(status_code=404, detail="Unknown app")
    proxy: DaemonProxy = request.app.state.daemon_proxy
    # The daemon keeps a crashed app in its slot (state "error") and refuses
    # every start until it is cleared, so clear it first.
    try:
        current = await _apps(request).current()
    except (httpx.HTTPError, ValueError):
        current = None
    if current is not None and current.get("state") == "error":
        with contextlib.suppress(httpx.RequestError):
            await proxy.request(
                "POST", "apps/stop-current-app", timeout=APP_CONTROL_TIMEOUT_S
            )
    await ensure_awake(request)
    try:
        response = await proxy.request(
            "POST", f"apps/start-app/{name}", timeout=APP_CONTROL_TIMEOUT_S
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="Reachy daemon unavailable") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        return {"error": detail or "Daemon refused the app", "name": name, "status": "refused"}
    await _apps(request).refresh_active()
    return {"name": name, "status": "loading", "ui_port": await _apps(request).ui_port(name)}


@router.post("/apps/stop/{name}")
async def stop_app(name: str, request: Request) -> dict[str, Any]:
    """Stop the daemon's current app (the daemon runs one at a time)."""
    proxy: DaemonProxy = request.app.state.daemon_proxy
    try:
        response = await proxy.request(
            "POST", "apps/stop-current-app", timeout=APP_CONTROL_TIMEOUT_S
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="Reachy daemon unavailable") from exc
    if response.status_code >= 400:
        return {"error": "No app is running", "name": name, "status": "stopped"}
    await _apps(request).refresh_active()
    return {"name": name, "status": "stopping"}


@router.get("/apps/{name}/ui")
async def app_ui(name: str, request: Request) -> dict[str, Any]:
    """Port of the app's own web UI and whether it is answering yet.

    Apps start their web server a few seconds after the process launches;
    the browser waits for ``ready`` before navigating so it never lands on a
    connection-refused page.
    """
    try:
        port = await _apps(request).ui_port(name)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Reachy daemon unavailable") from exc
    ready = False
    if port is not None:
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                await client.get(f"http://127.0.0.1:{port}/")
            ready = True
        except httpx.HTTPError:
            ready = False
    return {"name": name, "port": port, "ready": ready}
