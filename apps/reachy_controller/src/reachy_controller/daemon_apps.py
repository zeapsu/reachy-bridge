"""Adapter over the daemon's app manager (``/api/apps/*``).

The daemon installs community apps from Hugging Face Spaces into its own
``apps_venv`` and runs at most one of them at a time. This module only reads
and drives that state; the controller never spawns app processes itself.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from reachy_controller.daemon_proxy import DaemonProxy

# Daemon AppState -> the card states the dashboard already renders.
_STATE_MAP = {
    "starting": "loading",
    "running": "running",
    "stopping": "stopping",
    "error": "error",
    "done": "stopped",
}

_INSTALLED_TTL_S = 5.0


def _display_name(name: str) -> str:
    words = name.replace("-", "_").split("_")
    if words[:2] == ["reachy", "mini"]:
        words = words[2:] or words
    return " ".join(word.capitalize() for word in words if word)


class DaemonApps:
    """Installed-app list and current-app state, read from the daemon."""

    def __init__(self, proxy: DaemonProxy) -> None:
        self._proxy = proxy
        self._installed: list[dict[str, Any]] = []
        self._installed_at = 0.0
        self._current: dict[str, Any] | None = None
        self._current_name: str | None = None
        self._current_since: float | None = None
        self._lock = asyncio.Lock()

    async def _get(self, path: str) -> Any:
        response = await self._proxy.request("GET", path)
        response.raise_for_status()
        if not response.content.strip():
            return None
        return response.json()

    async def installed(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._installed and now - self._installed_at < _INSTALLED_TTL_S:
            return self._installed
        payload = await self._get("apps/list-available/installed")
        self._installed = payload if isinstance(payload, list) else []
        self._installed_at = now
        return self._installed

    async def current(self) -> dict[str, Any] | None:
        payload = await self._get("apps/current-app-status")
        current = payload if isinstance(payload, dict) else None
        name = (current or {}).get("info", {}).get("name")
        if name != self._current_name:
            self._current_name = name
            self._current_since = time.time() if name else None
        self._current = current
        return current

    def any_app_active(self) -> bool:
        """True when the last observed daemon app is starting or running."""
        if not self._current:
            return False
        return self._current.get("state") in ("starting", "running")

    async def refresh_active(self) -> bool:
        """Re-read the daemon before an actuation decision; fail closed."""
        try:
            await self.current()
        except (httpx.HTTPError, ValueError):
            return True
        return self.any_app_active()

    async def statuses(self) -> list[dict[str, Any]]:
        async with self._lock:
            installed, current = await asyncio.gather(self.installed(), self.current())
        current_name = (current or {}).get("info", {}).get("name")
        result = []
        for item in installed:
            name = str(item.get("name", ""))
            if not name:
                continue
            extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
            status = "stopped"
            error = None
            start_time = None
            if name == current_name and current is not None:
                status = _STATE_MAP.get(str(current.get("state")), "stopped")
                error = current.get("error")
                start_time = self._current_since if status == "running" else None
            result.append(
                {
                    "name": name,
                    "display": _display_name(name),
                    "blurb": item.get("description") or "",
                    "status": status,
                    "error": error,
                    "start_time": start_time,
                    "has_ui": bool(extra.get("custom_app_url")),
                }
            )
        return result

    async def ui_port(self, name: str) -> int | None:
        """Port of the app's own web UI, from its ``custom_app_url``."""
        for item in await self.installed():
            if item.get("name") != name:
                continue
            url = (item.get("extra") or {}).get("custom_app_url")
            if not url:
                return None
            try:
                return httpx.URL(url).port
            except (httpx.InvalidURL, TypeError):
                return None
        return None
