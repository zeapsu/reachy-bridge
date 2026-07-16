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
        self.goto_home = AsyncMock(return_value={"uuid": str(uuid4())})

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
    proxy.goto_home.return_value = {"uuid": wake_up_uuid}

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
    proxy.goto_home.assert_called_once()

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
    proxy.goto_home.return_value = {"uuid": wake_up_uuid}

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
    proxy.goto_home.assert_called_once()

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
    proxy.goto_home.return_value = {"uuid": wake_up_uuid}

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
    """Test 9: TWO CONSECUTIVE daemon errors mid-watch (running-list requests
    error back-to-back) → session cleared, no exception, no wake_up retry
    loop. One transient failure alone is now tolerated (see test 9b); this
    pins the still-unchanged give-up behavior once the daemon is actually gone.
    """
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy, poll_interval_s=0.01)

    test_uuid = "test-uuid-9"
    proxy.play_recorded_move.return_value = {"uuid": test_uuid}

    # First call is foreign check, second call has the move, third and
    # fourth calls return None back-to-back (daemon actually gone).
    proxy.get_running_moves.side_effect = [
        [],  # Foreign move check
        [{"uuid": test_uuid}],  # Move running
        None,  # First failure — tolerated
        None,  # Second consecutive failure — daemon confirmed gone
    ]

    result = await player.play("dataset1", "move1")
    assert result["status"] == "playing"

    # Wait for watcher to detect error and clear
    await asyncio.sleep(0.15)

    # Session should be cleared
    current = await player.current()
    assert current["active"] is False

    # wake_up should NOT be called
    proxy.goto_home.assert_not_called()

    await player.aclose()


@pytest.mark.asyncio
async def test_transient_poll_blip_during_playing_still_completes() -> None:
    """Test 9b: one transient None poll mid-playing-phase is tolerated —
    lifecycle still completes (wake_up called once, session cleared)."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy, poll_interval_s=0.01)

    test_uuid = "test-uuid-9b"
    wake_up_uuid = "wake-up-uuid-9b"
    proxy.play_recorded_move.return_value = {"uuid": test_uuid}
    proxy.goto_home.return_value = {"uuid": wake_up_uuid}

    proxy.get_running_moves.side_effect = [
        [],  # Foreign move check
        [{"uuid": test_uuid}],  # Still running
        None,  # Transient blip — tolerated
        [{"uuid": test_uuid}],  # Still running (retry succeeded)
        [],  # Finished
        [{"uuid": wake_up_uuid}],  # Wake_up running
        [],  # Wake_up finished
    ]

    result = await player.play("dataset1", "move1")
    assert result["status"] == "playing"

    await asyncio.sleep(0.2)

    proxy.goto_home.assert_called_once()

    current = await player.current()
    assert current["active"] is False

    await player.aclose()


@pytest.mark.asyncio
async def test_wake_up_post_transient_failure_still_returns_home() -> None:
    """Test 9c: goto_home returning None once is retried after one poll
    interval — return-home still happens, session eventually cleared."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy, poll_interval_s=0.01)

    test_uuid = "test-uuid-9c"
    wake_up_uuid = "wake-up-uuid-9c"
    proxy.play_recorded_move.return_value = {"uuid": test_uuid}
    proxy.goto_home = AsyncMock(side_effect=[None, {"uuid": wake_up_uuid}])

    proxy.get_running_moves.side_effect = [
        [],  # Foreign move check
        [{"uuid": test_uuid}],  # Still running
        [],  # Finished
        [{"uuid": wake_up_uuid}],  # Wake_up running
        [],  # Wake_up finished
    ]

    result = await player.play("dataset1", "move1")
    assert result["status"] == "playing"

    await asyncio.sleep(0.2)

    assert proxy.goto_home.call_count == 2

    current = await player.current()
    assert current["active"] is False

    await player.aclose()


@pytest.mark.asyncio
async def test_concurrent_plays_only_one_succeeds() -> None:
    """Test 10: two concurrent plays via asyncio.gather → exactly one daemon play POST, other gets 409."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy)

    uuid1 = "uuid1"
    proxy.play_recorded_move.return_value = {"uuid": uuid1}

    # AsyncMock resolves without ever suspending the calling task, so without a
    # real await point here asyncio.gather runs both play() calls back-to-back
    # on the same lock-holding pass and this test would pass even with the
    # `_lock` removed. `await asyncio.sleep(0)` forces an actual suspension so
    # the two plays interleave at the lock, making the assertions below
    # load-bearing against a missing/broken lock.
    async def yielding_running_moves(*args, **kwargs):
        await asyncio.sleep(0)
        return []

    proxy.get_running_moves.side_effect = yielding_running_moves

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
    """Test 11: play propagates daemon 404 (unknown move) → 404, session NOT left set.

    Drives a REAL DaemonProxy over httpx.MockTransport through the FastAPI app
    (not a mocked play_recorded_move) so this pins the actual HTTP-level status
    mapping: DaemonProxy.play_recorded_move only swallows transport errors, so
    the daemon's 404 must propagate as an httpx.HTTPStatusError that the route
    turns into a 404 response — not the 503 "unavailable" a naive _post_json
    reuse would produce. A follow-up play then must succeed, proving the
    lock/session were released on the 404 path (not wedged).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/move/play/recorded-move-dataset/dataset1/unknown_move":
            return httpx.Response(404, json={"detail": "Unknown move"})
        if path == "/api/move/play/recorded-move-dataset/dataset1/move1":
            return httpx.Response(200, json={"uuid": "test-uuid"})
        if path == "/api/move/running":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    proxy = DaemonProxy("http://daemon.local:8000", transport=httpx.MockTransport(handler))
    app = create_app(proxy)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/expressions/play/dataset1/unknown_move")
            assert response.status_code == 404

            current = await client.get("/api/expressions/current")
            assert current.json()["active"] is False

            # Session must not be wedged: a follow-up play for a real move succeeds.
            response2 = await client.post("/api/expressions/play/dataset1/move1")
            assert response2.status_code == 200
            assert response2.json()["status"] == "playing"


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


@pytest.mark.asyncio
async def test_stop_while_returning_home_is_noop() -> None:
    """Test 12: stop() while phase is returning_home returns {"status":
    "returning_home"} and fires no daemon stop call — this contract branch
    (frozen in the API contract as a 200 idempotent no-op) was previously
    unpinned by any test. Reaches into the session dict directly to land
    squarely in the returning_home window instead of racing the watcher's
    real timing.
    """
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy)
    player._session = {
        "uuid": "test-uuid",
        "dataset": "dataset1",
        "move": "move1",
        "phase": "returning_home",
    }

    result = await player.stop()
    assert result == {"status": "returning_home"}
    proxy.stop_move.assert_not_called()

    await player.aclose()


@pytest.mark.asyncio
async def test_return_home_creates_returning_home_session() -> None:
    """return_home() runs a session with no play half: goto fires, session
    clears when the goto uuid drains. Pins the null-field session shape the
    frontend sync-on-load path depends on (selector built from `move`)."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy, poll_interval_s=0.01)
    proxy.goto_home.return_value = {"uuid": "home-uuid"}
    proxy.get_running_moves.side_effect = [
        [{"uuid": "home-uuid"}],  # goto still running
        [],  # done
    ]

    result = await player.return_home()
    assert result == {"status": "returning_home"}

    current = await player.current()
    assert current["active"] is True
    assert current["phase"] == "returning_home"
    assert current["dataset"] is None
    assert current["move"] is None
    assert current["uuid"] is None

    await player._watcher
    assert (await player.current())["active"] is False
    proxy.goto_home.assert_called_once()
    await player.aclose()


@pytest.mark.asyncio
async def test_return_home_busy_when_session_active() -> None:
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy)
    proxy.play_recorded_move.return_value = {"uuid": "u1"}
    await player.play("dataset1", "move1")
    proxy.get_running_moves.return_value = [{"uuid": "u1"}]  # hold phase=playing

    result = await player.return_home()
    assert result["status"] == "busy"
    proxy.goto_home.assert_not_called()
    await player.aclose()


@pytest.mark.asyncio
async def test_stop_during_return_home_session_is_noop() -> None:
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy, poll_interval_s=0.01)
    proxy.goto_home.return_value = {"uuid": "home-uuid"}
    proxy.get_running_moves.return_value = [{"uuid": "home-uuid"}]  # goto in flight

    await player.return_home()
    result = await player.stop()
    assert result == {"status": "returning_home"}
    proxy.stop_move.assert_not_called()

    proxy.get_running_moves.return_value = []  # let it finish
    await player._watcher
    assert player.active is False
    await player.aclose()


@pytest.mark.asyncio
async def test_needs_home_blocks_play_with_actionable_detail() -> None:
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy)
    player.needs_home = True

    result = await player.play("dataset1", "move1")
    assert result["status"] == "busy"
    assert "enabled" in result["detail"]  # detail names the recovery action
    proxy.play_recorded_move.assert_not_called()
    await player.aclose()


@pytest.mark.asyncio
async def test_needs_home_persists_when_goto_dispatch_fails() -> None:
    """SAFETY: goto dispatch fails twice -> session clears but the flag stays
    set, so expressions keep refusing (safe direction)."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy, poll_interval_s=0.01)
    player.needs_home = True
    proxy.goto_home.return_value = None  # both dispatch attempts fail

    await player.return_home()
    await player._watcher

    assert player.active is False
    assert player.needs_home is True
    result = await player.play("dataset1", "move1")
    assert result["status"] == "busy"
    await player.aclose()


@pytest.mark.asyncio
async def test_home_completion_clears_needs_home() -> None:
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy, poll_interval_s=0.01)
    player.needs_home = True
    proxy.goto_home.return_value = {"uuid": "home-uuid"}
    proxy.get_running_moves.side_effect = [
        [{"uuid": "home-uuid"}],
        [],
    ]

    await player.return_home()
    await player._watcher
    assert player.needs_home is False

    proxy.get_running_moves.side_effect = None
    proxy.get_running_moves.return_value = []
    result = await player.play("dataset1", "move1")
    assert result["status"] == "playing"
    await player.aclose()


@pytest.mark.asyncio
async def test_mode_change_mid_goto_keeps_needs_home() -> None:
    """SAFETY: a motor-mode change while the goto-home is in flight means the
    drained goto never physically homed the robot (torque may be off) — the
    flag must survive and keep refusing plays."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy, poll_interval_s=0.01)
    player.needs_home = True
    proxy.goto_home.return_value = {"uuid": "home-uuid"}
    proxy.get_running_moves.return_value = [{"uuid": "home-uuid"}]  # goto in flight

    await player.return_home()
    # Let the watcher task actually start and dispatch the goto (asyncio
    # only runs a freshly created task once its creator yields control —
    # without this checkpoint the "mode change" below would race ahead of
    # the goto's own epoch capture instead of landing mid-flight).
    await asyncio.sleep(0)
    # Simulate the motor route reacting to a mid-flight mode change.
    player.mode_epoch += 1
    player.needs_home = True

    proxy.get_running_moves.return_value = []  # goto drains on schedule
    await player._watcher

    assert player.active is False
    assert player.needs_home is True  # NOT cleared by the stale goto
    result = await player.play("dataset1", "move1")
    assert result["status"] == "busy"
    await player.aclose()
