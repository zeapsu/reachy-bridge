"""Focused tests for app metadata and persisted favorites."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from reachy_controller.app_supervisor import AppSupervisor
from reachy_controller.main import create_app


class FakeDaemonProxy:
    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _build_app(tmp_path: Path) -> Any:
    app = create_app(proxy=FakeDaemonProxy())
    app.state.app_supervisor._workspace_root = tmp_path
    return app


async def _request(app: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://controller.test",
        ) as client:
            return await client.request(method, path, **kwargs)
    finally:
        await app.state.app_settings_proxy.close()


@pytest.mark.asyncio
async def test_favorites_default_and_corrupt_fallback(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    default = await _request(app, "GET", "/api/apps/favorites")
    assert default.status_code == 200
    assert default.json() == {"favorites": AppSupervisor.DEFAULT_FAVORITES}

    (tmp_path / ".favorites.json").write_text("{not json", encoding="utf-8")
    corrupt = await _request(app, "GET", "/api/apps/favorites")
    assert corrupt.status_code == 200
    assert corrupt.json() == {"favorites": AppSupervisor.DEFAULT_FAVORITES}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("favorites", "offending"),
    [
        (["reachy_chat", "reachy_dance", "reachy_lookat", "reachy_simon"], "reachy_simon"),
        (["reachy_chat", "reachy_chat"], "reachy_chat"),
        (["reachy_chat", "not_registered"], "not_registered"),
    ],
)
async def test_favorites_validation_names_offending_entry(
    tmp_path: Path, favorites: list[str], offending: str
) -> None:
    app = _build_app(tmp_path)
    response = await _request(app, "PUT", "/api/apps/favorites", json={"favorites": favorites})
    assert response.status_code == 422
    assert offending in response.json()["detail"]
    assert not (tmp_path / ".favorites.json").exists()


@pytest.mark.asyncio
async def test_favorites_atomic_roundtrip_and_restart(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    chosen = ["reachy_simon", "reachy_chat"]
    response = await _request(app, "PUT", "/api/apps/favorites", json={"favorites": chosen})
    assert response.status_code == 200
    assert response.json() == {"favorites": chosen}

    path = tmp_path / ".favorites.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"favorites": chosen}
    assert not list(tmp_path.glob(".favorites.json.*.tmp"))

    restarted = AppSupervisor()
    restarted._workspace_root = tmp_path
    assert restarted.get_favorites() == chosen


@pytest.mark.asyncio
async def test_apps_status_includes_display_and_blurb_metadata(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    response = await _request(app, "GET", "/api/apps/status")
    assert response.status_code == 200
    statuses = {status["name"]: status for status in response.json()}
    assert set(statuses) == set(AppSupervisor.APPS)
    for name, config in AppSupervisor.APPS.items():
        assert statuses[name]["display"] == config["display"]
        assert statuses[name]["blurb"] == config["blurb"]
