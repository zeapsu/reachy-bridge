"""Tests for daemon control routes."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from reachy_controller.daemon_proxy import DaemonProxy
from reachy_controller.main import create_app


class DaemonControlProxy(DaemonProxy):
    """Mock DaemonProxy for testing daemon control routes."""

    def __init__(self, running: bool = False) -> None:
        self.running = running
        self.acquire_media = AsyncMock(return_value={"ok": True})
        self.get_speaker_volume = AsyncMock(return_value={"ok": True})
        self.set_speaker_volume = AsyncMock(return_value={"ok": True})
        self.get_microphone_volume = AsyncMock(return_value={"ok": True})
        self.set_microphone_volume = AsyncMock(return_value={"ok": True})
        self.get_motor_status = AsyncMock(return_value={"ok": True})
        self.set_motor_mode = AsyncMock(return_value={"ok": True})
        self.enable_wobbling = AsyncMock(return_value={"ok": True})
        self.disable_wobbling = AsyncMock(return_value={"ok": True})
        self.play_sound = AsyncMock(return_value={"ok": True})
        self.play_test_sound = AsyncMock(return_value={"ok": True})
        self.stop_sound = AsyncMock(return_value={"ok": True})
        self.release_media = AsyncMock(return_value={"ok": True})
        self.get_media_status = AsyncMock(return_value={"ok": True})

    async def get_full_state(self) -> dict | None:
        """Mock get_full_state to control reachability."""
        return {"state": "ok"} if self.running else None

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_daemon_status_returns_manageable_and_running() -> None:
    """Test GET /api/daemon/status returns expected fields."""
    import os
    from unittest.mock import patch

    proxy = DaemonControlProxy(running=True)
    with patch.dict(os.environ, {"REACHY_MANAGE_DAEMON": "1"}):
        app = create_app(proxy=proxy)  # type: ignore[arg-type]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/daemon/status")

    assert response.status_code == 200
    data = response.json()
    assert "running" in data
    assert "manageable" in data
    assert "status" in data
    assert "pid" in data
    assert "start_time" in data
    assert data["running"] is True
    assert data["manageable"] is True


@pytest.mark.asyncio
async def test_daemon_status_reports_not_running_when_unreachable() -> None:
    """Test that status reports running=False when daemon is unreachable."""
    import os
    from unittest.mock import patch

    proxy = DaemonControlProxy(running=False)
    with patch.dict(os.environ, {"REACHY_MANAGE_DAEMON": "1"}):
        app = create_app(proxy=proxy)  # type: ignore[arg-type]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/daemon/status")

    assert response.status_code == 200
    data = response.json()
    assert data["running"] is False


@pytest.mark.asyncio
async def test_daemon_status_not_manageable() -> None:
    """Test daemon status when REACHY_MANAGE_DAEMON is not set."""
    proxy = DaemonControlProxy(running=False)
    app = create_app(proxy=proxy)  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/daemon/status")

    assert response.status_code == 200
    data = response.json()
    assert data["manageable"] is False


@pytest.mark.asyncio
async def test_daemon_start_invokes_supervisor() -> None:
    """Test POST /api/daemon/start invokes the supervisor."""
    import os
    from unittest.mock import patch

    proxy = DaemonControlProxy()
    with patch.dict(os.environ, {"REACHY_MANAGE_DAEMON": "1"}):
        app = create_app(proxy=proxy)  # type: ignore[arg-type]

        # Mock the supervisor's start method
        mock_start = AsyncMock(return_value={"status": "starting", "pid": 12345})
        app.state.daemon_supervisor.start = mock_start

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/daemon/start")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "starting"
    mock_start.assert_awaited_once()


@pytest.mark.asyncio
async def test_daemon_stop_invokes_supervisor() -> None:
    """Test POST /api/daemon/stop invokes the supervisor."""
    import os
    from unittest.mock import patch

    proxy = DaemonControlProxy()
    with patch.dict(os.environ, {"REACHY_MANAGE_DAEMON": "1"}):
        app = create_app(proxy=proxy)  # type: ignore[arg-type]

        # Mock the supervisor's stop method
        mock_stop = AsyncMock(return_value={"status": "stopped"})
        app.state.daemon_supervisor.stop = mock_stop

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/daemon/stop")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "stopped"
    mock_stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_daemon_status_cache_coalesces_within_ttl() -> None:
    """Test that rapid status calls within TTL reuse the same probe result.

    Two GET /api/daemon/status calls within the TTL window should cause
    proxy.get_full_state to be awaited only once.
    """
    import os
    from unittest.mock import patch

    proxy = DaemonControlProxy(running=True)
    with patch.dict(os.environ, {"REACHY_MANAGE_DAEMON": "1"}):
        app = create_app(proxy=proxy)  # type: ignore[arg-type]

        # Spy on get_full_state to count calls
        original_get_full_state = proxy.get_full_state
        call_count = 0

        async def spy_get_full_state(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            return await original_get_full_state(*args, **kwargs)

        proxy.get_full_state = spy_get_full_state

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # First call
            response1 = await client.get("/api/daemon/status")
            assert response1.status_code == 200
            assert response1.json()["running"] is True

            # Second call within TTL (should reuse cache)
            response2 = await client.get("/api/daemon/status")
            assert response2.status_code == 200
            assert response2.json()["running"] is True

        # Verify get_full_state was called only once
        assert call_count == 1, f"Expected 1 probe call, got {call_count}"


@pytest.mark.asyncio
async def test_daemon_status_cache_reprobe_after_ttl() -> None:
    """Test that after TTL expires, the next call re-probes and reflects new state.

    After advancing the clock past the TTL window, a new call should trigger
    a fresh probe that reflects any state change.
    """
    import os
    from unittest.mock import patch

    proxy = DaemonControlProxy(running=True)
    with patch.dict(os.environ, {"REACHY_MANAGE_DAEMON": "1"}):
        app = create_app(proxy=proxy)  # type: ignore[arg-type]

        # Spy on get_full_state to count calls
        original_get_full_state = proxy.get_full_state
        call_count = 0

        async def spy_get_full_state(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            return await original_get_full_state(*args, **kwargs)

        proxy.get_full_state = spy_get_full_state

        # Patch time.monotonic to control the clock
        fake_time = [0.0]  # Use list for mutability in nested scope

        def mock_monotonic() -> float:  # type: ignore[no-untyped-def]
            return fake_time[0]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            with patch("time.monotonic", side_effect=mock_monotonic):
                # First call
                response1 = await client.get("/api/daemon/status")
                assert response1.status_code == 200
                assert response1.json()["running"] is True
                assert call_count == 1

                # Advance clock past TTL (1.0 second)
                fake_time[0] = 1.5

                # Change daemon state
                proxy.running = False

                # Second call after TTL should re-probe
                response2 = await client.get("/api/daemon/status")
                assert response2.status_code == 200
                # Should reflect the new state (now unreachable)
                assert response2.json()["running"] is False

                # Verify get_full_state was called a second time
                assert call_count == 2, f"Expected 2 probe calls, got {call_count}"
