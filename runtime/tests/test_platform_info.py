"""The platform report at /api/bridge/platform (#140)."""

from __future__ import annotations

import sys
import types

import httpx
import pytest
from fastapi import FastAPI

from reachy_bridge_runtime import main as runtime_main
from reachy_bridge_runtime import platforms, upstream
from reachy_bridge_runtime.features import platform_info


@pytest.fixture(autouse=True)
def _fake_daemon_app(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    monkeypatch.setattr(upstream, "_app_installers", [])
    monkeypatch.setattr(upstream, "installed_version", lambda: "9.9.9")
    monkeypatch.setattr(platforms, "device_model", lambda: "Test Board Rev 1\x00")
    daemon_main = types.ModuleType("reachy_mini.daemon.app.main")
    daemon_main.create_app = lambda *_args, **_kwargs: FastAPI()
    package = types.ModuleType("reachy_mini.daemon.app")
    package.main = daemon_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "reachy_mini.daemon.app", package)
    monkeypatch.setitem(sys.modules, "reachy_mini.daemon.app.main", daemon_main)
    return daemon_main


async def _get(app: FastAPI) -> dict:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://d") as client:
        response = await client.get("/api/bridge/platform")
    assert response.status_code == 200
    return response.json()


@pytest.mark.asyncio
async def test_route_reports_board_tier_and_features(_fake_daemon_app: types.ModuleType) -> None:
    board = platforms.Platform(name="test-board", tier="experimental", features=frozenset({"camera"}))
    platform_info.apply(board, active=["keep_awake", "camera"], skipped=["kinematics"])
    assert await _get(_fake_daemon_app.create_app()) == {
        "name": "test-board",
        "tier": "experimental",
        "model": "Test Board Rev 1",
        "features": ["camera", "keep_awake"],
        "skipped": ["kinematics"],
        "reachy_mini": "9.9.9",
    }


@pytest.mark.asyncio
async def test_startup_reports_what_apply_skipped(
    _fake_daemon_app: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = platforms.Platform(name="test-board", tier="tested", features=frozenset({"audio_levels"}))
    runtime_main.report_platform(board, skipped=["keep_awake"], routes_ok=True)
    body = await _get(_fake_daemon_app.create_app())
    assert body["features"] == ["app_bind", "audio_levels", "idle_reset", "kinematics", "media_return"]
    assert body["skipped"] == ["keep_awake"]


def test_startup_leaves_the_report_out_when_routes_dont_match(_fake_daemon_app: types.ModuleType) -> None:
    runtime_main.report_platform(platforms.Platform(name="x", tier="tested"), skipped=[], routes_ok=False)
    assert upstream._app_installers == []
