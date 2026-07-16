from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import psutil
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


@pytest.mark.asyncio
async def test_camera_available_with_no_apps_running() -> None:
    supervisor = AppSupervisor()

    assert supervisor.camera_available() is True
    assert supervisor.camera_holder() is None


@pytest.mark.asyncio
async def test_non_camera_app_keeps_camera_available() -> None:
    supervisor = AppSupervisor()
    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        await supervisor.start_app("reachy_dance")

    assert supervisor.camera_available() is True
    assert supervisor.camera_holder() is None


@pytest.mark.asyncio
async def test_camera_app_holds_camera() -> None:
    supervisor = AppSupervisor()
    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        await supervisor.start_app("reachy_chat")

    assert supervisor.camera_available() is False
    assert supervisor.camera_holder() == "reachy_chat"


@pytest.mark.asyncio
async def test_stopping_camera_app_restores_camera_availability() -> None:
    supervisor = AppSupervisor()
    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        await supervisor.start_app("reachy_chat")
        result = await supervisor.stop_app("reachy_chat")

    assert result["status"] == "stopped"
    assert supervisor.camera_available() is True
    assert supervisor.camera_holder() is None


def test_stopping_camera_app_remains_camera_holder_until_stopped() -> None:
    supervisor = AppSupervisor()
    supervisor._apps["reachy_chat"].status = "stopping"

    assert supervisor.camera_holder() == "reachy_chat"
    assert supervisor.camera_available() is False


def test_stopping_camera_app_releases_holder_after_process_exit() -> None:
    supervisor = AppSupervisor()
    app = supervisor._apps["reachy_chat"]
    app.status = "stopping"
    app.pid = 4242

    with patch(
        "reachy_controller.app_supervisor.psutil.Process",
        side_effect=psutil.NoSuchProcess(4242),
    ):
        supervisor.get_all_status()

    assert app.status == "stopped"
    assert supervisor.camera_holder() is None

@pytest.mark.asyncio
async def test_camera_release_hook_runs_before_spawning_camera_app() -> None:
    supervisor = AppSupervisor()
    calls: list[str] = []

    async def release_camera() -> None:
        calls.append("release")

    def spawn(*_: object, **__: object) -> MagicMock:
        calls.append("spawn")
        mock_process = MagicMock()
        mock_process.pid = 12345
        return mock_process

    supervisor.set_camera_release_hook(release_camera)

    with patch("reachy_controller.app_supervisor.subprocess.Popen", side_effect=spawn):
        await supervisor.start_app("reachy_chat")

    assert calls == ["release", "spawn"]


@pytest.mark.asyncio
async def test_camera_release_hook_does_not_run_for_non_camera_app() -> None:
    supervisor = AppSupervisor()
    release_camera = AsyncMock()
    mock_process = MagicMock()
    mock_process.pid = 12345
    supervisor.set_camera_release_hook(release_camera)

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        await supervisor.start_app("reachy_dance")

    release_camera.assert_not_awaited()


@pytest.mark.asyncio
async def test_camera_availability_route_reports_running_camera_app() -> None:
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
        "available": False,
        "reason": "in_use",
        "holder": "reachy_chat",
    }
    proxy.get_daemon_status.assert_not_awaited()
    proxy.get_media_status.assert_not_awaited()


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
