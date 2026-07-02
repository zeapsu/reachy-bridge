"""Tests for expression playback lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from reachy_controller.daemon_proxy import DaemonProxy
from reachy_controller.main import create_app
from reachy_controller.routes.expressions import ExpressionPlayer


class MockDaemonProxy:
    """Mock proxy for expression player tests."""

    def __init__(self) -> None:
        self.result = {"ok": True}
        self.play_recorded_move = AsyncMock(return_value={"uuid": str(uuid4())})
        self.get_running_moves = AsyncMock(return_value=[])
        self.stop_move = AsyncMock(return_value=None)
        self.play_wake_up = AsyncMock(return_value={"uuid": str(uuid4())})

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_play_returns_session_with_uuid() -> None:
    """Test 1: play → daemon receives POST play, 200 with uuid, `current` reports playing."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy)
    test_uuid = "test-uuid-1"
    proxy.play_recorded_move.return_value = {"uuid": test_uuid}

    result = await player.play("dataset1", "move1")

    assert result["status"] == "playing"
    assert result["uuid"] == test_uuid
    assert result["dataset"] == "dataset1"
    assert result["move"] == "move1"

    # Check current reports playing
    current = await player.current()
    assert current["active"] is True
    assert current["phase"] == "playing"
    assert current["uuid"] == test_uuid
    assert current["dataset"] == "dataset1"
    assert current["move"] == "move1"

    proxy.play_recorded_move.assert_called_once_with("dataset1", "move1")
    await player.aclose()


@pytest.mark.asyncio
async def test_play_while_session_active_returns_409() -> None:
    """Test 2: play while session active → 409, daemon play called exactly once."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy)
    proxy.play_recorded_move.return_value = {"uuid": "uuid1"}

    # Start first play
    result1 = await player.play("dataset1", "move1")
    assert result1["status"] == "playing"

    # Try second play while active
    result2 = await player.play("dataset2", "move2")
    assert result2["status"] == "busy"
    assert result2["detail"] == "Robot is busy with another move."

    # Daemon play should be called exactly once
    proxy.play_recorded_move.assert_called_once_with("dataset1", "move1")

    await player.aclose()


@pytest.mark.asyncio
async def test_play_with_foreign_running_move_returns_409() -> None:
    """Test 3: play while daemon reports a foreign running move → 409, no play POST."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy)

    # Daemon reports a foreign move already running
    proxy.get_running_moves.return_value = [{"uuid": "foreign-uuid"}]

    result = await player.play("dataset1", "move1")
    assert result["status"] == "busy"
    assert result["detail"] == "Robot is busy with another move."

    # Play should not be called
    proxy.play_recorded_move.assert_not_called()

    await player.aclose()


@pytest.mark.asyncio
async def test_natural_finish_triggers_wake_up() -> None:
    """Test 4: natural finish → fake daemon running-list sequence [ [uuid], [] ] → watcher POSTs wake_up → after wake_up's uuid drains, `current` → inactive."""
    proxy = MockDaemonProxy()
    # Set short poll interval for testing
    player = ExpressionPlayer(proxy, poll_interval_s=0.01)

    test_uuid = "test-uuid-4"
    wake_up_uuid = "wake-up-uuid-4"
    proxy.play_recorded_move.return_value = {"uuid": test_uuid}
    proxy.play_wake_up.return_value = {"uuid": wake_up_uuid}

    # Simulate daemon: note first call (during play()) checks for foreign moves
    proxy.get_running_moves.side_effect = [
        [],  # Foreign move check (empty)
        [{"uuid": test_uuid}],  # Move still running
        [],  # Move finished, watcher will trigger wake_up
        [{"uuid": wake_up_uuid}],  # Wake_up now running
        [],  # Wake_up finished
    ]

    result = await player.play("dataset1", "move1")
    assert result["status"] == "playing"

    # Wait for watcher to complete the cycle
    await asyncio.sleep(0.15)

    # Verify wake_up was called
    proxy.play_wake_up.assert_called_once()

    # Final state should be inactive
    current = await player.current()
    assert current["active"] is False

    await player.aclose()


@pytest.mark.asyncio
async def test_stop_while_playing_triggers_return_home() -> None:
    """Test 5: stop while playing → daemon /move/stop POSTed with the uuid; return-home still happens; final state inactive."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy, poll_interval_s=0.01)

    test_uuid = "test-uuid-5"
    wake_up_uuid = "wake-up-uuid-5"
    proxy.play_recorded_move.return_value = {"uuid": test_uuid}
    proxy.play_wake_up.return_value = {"uuid": wake_up_uuid}

    # Sequence: first call is foreign move check, then move running, stops, wake_up runs, finishes
    proxy.get_running_moves.side_effect = [
        [],  # Foreign move check
        [{"uuid": test_uuid}],  # Move still running
        [],  # After stop, move is gone
        [{"uuid": wake_up_uuid}],  # Wake_up now running
        [],  # Wake_up finished
    ]

    result = await player.play("dataset1", "move1")
    assert result["status"] == "playing"

    # Call stop
    stop_result = await player.stop()
    assert stop_result["status"] == "stopping"

    # Verify stop_move was called
    proxy.stop_move.assert_called_once_with(test_uuid)

    # Wait for watcher to complete
    await asyncio.sleep(0.15)

    # Verify wake_up was called
    proxy.play_wake_up.assert_called_once()

    # Final state should be inactive
    current = await player.current()
    assert current["active"] is False

    await player.aclose()


@pytest.mark.asyncio
async def test_stop_tolerates_daemon_500() -> None:
    """Test 6: stop tolerates daemon 500 on /move/stop (move just finished): still returns "stopping", watcher completes."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy, poll_interval_s=0.01)

    test_uuid = "test-uuid-6"
    wake_up_uuid = "wake-up-uuid-6"
    proxy.play_recorded_move.return_value = {"uuid": test_uuid}
    proxy.play_wake_up.return_value = {"uuid": wake_up_uuid}

    # stop_move returns None (daemon's 500 was swallowed by proxy)
    proxy.stop_move = AsyncMock(return_value=None)

    # Sequence: first call is foreign move check, then move finishes naturally while we're trying to stop
    proxy.get_running_moves.side_effect = [
        [],  # Foreign move check
        [{"uuid": test_uuid}],
        [],  # Already finished (or will be soon)
        [{"uuid": wake_up_uuid}],
        [],
    ]

    result = await player.play("dataset1", "move1")
    assert result["status"] == "playing"

    # Call stop (returns "stopping" even if daemon had 500)
    stop_result = await player.stop()
    assert stop_result["status"] == "stopping"

    # Wait for watcher to complete
    await asyncio.sleep(0.15)

    # Final state should be inactive
    current = await player.current()
    assert current["active"] is False

    await player.aclose()


@pytest.mark.asyncio
async def test_stop_when_idle_returns_idle() -> None:
    """Test 7: stop when idle → {"status": "idle"}, zero daemon calls."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy)

    result = await player.stop()
    assert result["status"] == "idle"

    # No daemon calls should have been made
    proxy.stop_move.assert_not_called()

    await player.aclose()


@pytest.mark.asyncio
async def test_play_with_daemon_unreachable_returns_503() -> None:
    """Test 8: play with daemon unreachable (transport raises httpx.ConnectError) → 503."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy)

    # Make get_running_moves return None (simulating daemon unreachable)
    proxy.get_running_moves = AsyncMock(return_value=None)

    result = await player.play("dataset1", "move1")
    assert result["status"] == "unavailable"
    assert result["detail"] == "Reachy daemon unavailable"

    await player.aclose()


@pytest.mark.asyncio
async def test_daemon_dies_mid_watch_clears_session() -> None:
    """Test 9: daemon dies mid-watch (running-list request errors) → session cleared, no exception, no wake_up retry loop."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy, poll_interval_s=0.01)

    test_uuid = "test-uuid-9"
    proxy.play_recorded_move.return_value = {"uuid": test_uuid}

    # First call is foreign check, second call has the move, third call returns None (daemon dies)
    proxy.get_running_moves.side_effect = [
        [],  # Foreign move check
        [{"uuid": test_uuid}],  # Move running
        None,  # Daemon dies
    ]

    result = await player.play("dataset1", "move1")
    assert result["status"] == "playing"

    # Wait for watcher to detect error and clear
    await asyncio.sleep(0.15)

    # Session should be cleared
    current = await player.current()
    assert current["active"] is False

    # wake_up should NOT be called
    proxy.play_wake_up.assert_not_called()

    await player.aclose()


@pytest.mark.asyncio
async def test_concurrent_plays_only_one_succeeds() -> None:
    """Test 10: two concurrent plays via asyncio.gather → exactly one daemon play POST, other gets 409."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy)

    uuid1 = "uuid1"
    proxy.play_recorded_move.return_value = {"uuid": uuid1}

    # Execute both plays concurrently
    results = await asyncio.gather(
        player.play("dataset1", "move1"),
        player.play("dataset2", "move2"),
    )

    # One should succeed, one should fail with 409
    statuses = [r["status"] for r in results]
    assert statuses.count("playing") == 1
    assert statuses.count("busy") == 1

    # Daemon play should be called exactly once
    proxy.play_recorded_move.assert_called_once()

    await player.aclose()


@pytest.mark.asyncio
async def test_play_propagates_daemon_404() -> None:
    """Test 11: play propagates daemon 404 (unknown move) → 404, session NOT left set."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy)

    # Make play_recorded_move raise a 404
    async def play_404(*args):
        response = httpx.Response(404)
        raise httpx.HTTPStatusError("404", request=None, response=response)

    proxy.play_recorded_move = AsyncMock(side_effect=play_404)

    result = await player.play("dataset1", "unknown_move")
    assert result["status"] == "not_found"

    # Session should not be set
    current = await player.current()
    assert current["active"] is False

    await player.aclose()


@pytest.mark.asyncio
async def test_play_returns_session_in_app() -> None:
    """Test that the play endpoint works through the app."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/move/play/recorded-move-dataset/dataset1/move1":
            return httpx.Response(200, json={"uuid": "test-uuid"})
        elif path == "/api/move/running":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    proxy = DaemonProxy("http://daemon.local:8000", transport=httpx.MockTransport(handler))
    app = create_app(proxy)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/expressions/play/dataset1/move1")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "playing"
            assert data["uuid"] == "test-uuid"
