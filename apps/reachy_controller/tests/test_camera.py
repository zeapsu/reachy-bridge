from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from reachy_controller.app_supervisor import AppSupervisor
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


def test_app_status_exposes_camera_capability_without_ownership() -> None:
    supervisor = AppSupervisor()

    statuses = {item["name"]: item for item in supervisor.get_all_status()}

    assert statuses["reachy_chat"]["uses_camera"] is True
    assert statuses["reachy_lookat"]["uses_camera"] is True
    assert statuses["reachy_dance"]["uses_camera"] is False
    assert statuses["reachy_simon"]["uses_camera"] is False
    assert statuses["reachy_hermes"]["uses_camera"] is False


@pytest.mark.asyncio
async def test_vision_app_starts_with_camera_capability_metadata() -> None:
    supervisor = AppSupervisor()
    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        result = await supervisor.start_app("reachy_chat")

    assert result["status"] == "loading"
    assert supervisor._apps["reachy_chat"].uses_camera is True


@pytest.mark.asyncio
async def test_camera_availability_stays_available_with_running_vision_app() -> None:
    proxy = CameraProxy(daemon_status={"ready": True}, media_status={"owner": "daemon"})
    app = create_app(proxy=proxy)  # type: ignore[arg-type]
    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        await app.state.app_supervisor.start_app("reachy_chat")

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
