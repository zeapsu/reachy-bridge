from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from reachy_controller.main import create_app


class CameraProxy:
    def __init__(
        self,
        *,
        daemon_status: dict[str, object] | None = None,
        media_status: dict[str, object] | None = None,
    ) -> None:
        self.get_daemon_status = AsyncMock(return_value=daemon_status)
        self.get_media_status = AsyncMock(return_value=media_status)
        self.get_robot_app_lock_status = AsyncMock(return_value={"state": "free"})

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_camera_availability_stays_available_with_running_vision_app() -> None:
    proxy = CameraProxy(daemon_status={"ready": True}, media_status={"owner": "daemon"})
    app = create_app(proxy=proxy)  # type: ignore[arg-type]
    app.state.daemon_apps._current = {"info": {"name": "vision_app"}, "state": "running"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/camera/availability")

    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "reason": "available",
        "holder": None,
    }
    proxy.get_media_status.assert_awaited_once()
    proxy.get_daemon_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_camera_availability_route_reports_daemon_down() -> None:
    proxy = CameraProxy(daemon_status=None, media_status=None)
    app = create_app(proxy=proxy)  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/camera/availability")

    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "reason": "daemon_down",
        "holder": None,
    }
    proxy.get_media_status.assert_awaited_once()
