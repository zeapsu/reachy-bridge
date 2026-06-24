"""Tests for AppSupervisor."""

from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from reachy_controller.app_supervisor import AppSupervisor, LocalApp


@pytest.mark.asyncio
async def test_start_app_success() -> None:
    """Test successful app start."""
    supervisor = AppSupervisor()

    # Mock subprocess.Popen
    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        result = await supervisor.start_app("reachy_chat")

    assert result["status"] == "running"
    assert result["name"] == "reachy_chat"
    assert result["pid"] == 12345
    assert supervisor._apps["reachy_chat"].status == "running"


@pytest.mark.asyncio
async def test_start_app_unknown_app() -> None:
    """Test start with unknown app name."""
    supervisor = AppSupervisor()
    result = await supervisor.start_app("unknown_app")
    assert result.get("status") == 404
    assert "Unknown app" in result.get("error", "")


@pytest.mark.asyncio
async def test_single_app_lock() -> None:
    """Test that only one app can run at a time."""
    supervisor = AppSupervisor()

    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        # Start first app
        result1 = await supervisor.start_app("reachy_chat")
        assert result1["status"] == "running"

        # Try to start second app
        result2 = await supervisor.start_app("reachy_dance")
        assert result2["status"] == "refused"
        assert "already running" in result2.get("error", "")


@pytest.mark.asyncio
async def test_stop_app() -> None:
    """Test stopping an app."""
    supervisor = AppSupervisor()

    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        await supervisor.start_app("reachy_chat")
        result = await supervisor.stop_app("reachy_chat")

    assert result["status"] == "stopped"
    assert supervisor._apps["reachy_chat"].status == "stopped"
    mock_process.send_signal.assert_called_once()


@pytest.mark.asyncio
async def test_stop_app_unknown() -> None:
    """Test stop with unknown app name."""
    supervisor = AppSupervisor()
    result = await supervisor.stop_app("unknown_app")
    assert result.get("status") == 404


@pytest.mark.asyncio
async def test_daemon_lock_check() -> None:
    """Test that start is refused if daemon reports lock held."""
    mock_proxy = AsyncMock()
    mock_proxy.get_robot_app_lock_status = AsyncMock(
        return_value={"state": "local_app", "holder_name": "other_app"}
    )
    supervisor = AppSupervisor(daemon_proxy=mock_proxy)

    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        result = await supervisor.start_app("reachy_chat")

    assert result["status"] == "refused"
    assert "Robot held by" in result.get("error", "")
    mock_process.assert_not_called()


@pytest.mark.asyncio
async def test_get_all_status() -> None:
    """Test getting status for all apps."""
    supervisor = AppSupervisor()
    status = supervisor.get_all_status()

    assert len(status) == 4
    assert all(s["status"] == "stopped" for s in status)
    assert set(s["name"] for s in status) == {
        "reachy_chat",
        "reachy_dance",
        "reachy_lookat",
        "reachy_simon",
    }


@pytest.mark.asyncio
async def test_get_status_single() -> None:
    """Test getting status for one app."""
    supervisor = AppSupervisor()
    status = supervisor.get_status("reachy_chat")

    assert status is not None
    assert status["name"] == "reachy_chat"
    assert status["status"] == "stopped"


def test_get_status_unknown() -> None:
    """Test getting status for unknown app."""
    supervisor = AppSupervisor()
    status = supervisor.get_status("unknown_app")
    assert status is None


def test_stop_all() -> None:
    """Test stopping all apps."""
    supervisor = AppSupervisor()

    # Manually set up mock processes for two apps
    mock_process1 = MagicMock()
    mock_process2 = MagicMock()
    supervisor._apps["reachy_chat"].process = mock_process1
    supervisor._apps["reachy_chat"].status = "running"
    supervisor._apps["reachy_dance"].process = mock_process2
    supervisor._apps["reachy_dance"].status = "running"

    supervisor.stop_all()

    assert supervisor._apps["reachy_chat"].status == "stopped"
    assert supervisor._apps["reachy_dance"].status == "stopped"
    mock_process1.terminate.assert_called_once()
    mock_process2.terminate.assert_called_once()


def test_local_app_to_dict() -> None:
    """Test LocalApp.to_dict() serialization."""
    app = LocalApp(
        name="reachy_chat",
        module="reachy_chat",
        pid=12345,
        status="running",
        error=None,
        start_time=1234567890.0,
    )
    data = app.to_dict()

    assert data["name"] == "reachy_chat"
    assert data["pid"] == 12345
    assert data["status"] == "running"
    assert data["error"] is None
