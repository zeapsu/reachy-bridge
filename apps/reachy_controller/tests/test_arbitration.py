"""Cross-surface arbitration: apps <-> expressions (#66) + motor-enable return-home (#67)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from reachy_controller.app_supervisor import AppSupervisor
from reachy_controller.daemon_proxy import DaemonProxy
from reachy_controller.main import create_app
from reachy_controller.routes.expressions import ExpressionPlayer


def _make_app(handler):
    proxy = DaemonProxy("http://daemon.local:8000", transport=httpx.MockTransport(handler))
    return create_app(proxy)


def test_any_app_active_states() -> None:
    supervisor = AppSupervisor()
    assert supervisor.any_app_active() is False
    for status, expected in (
        ("running", True),
        ("loading", True),
        ("stopped", False),
        ("stopping", False),
        ("error", False),
    ):
        supervisor._apps["reachy_chat"].status = status
        assert supervisor.any_app_active() is expected, status


def test_expression_player_active_property() -> None:
    player = ExpressionPlayer(AsyncMock())
    assert player.active is False
    player._session = {"uuid": None, "dataset": None, "move": None, "phase": "returning_home"}
    assert player.active is True


@pytest.mark.asyncio
async def test_play_expression_409_while_app_running() -> None:
    play_posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/move/play/"):
            play_posts.append(request.url.path)
            return httpx.Response(200, json={"uuid": "u1"})
        if request.url.path == "/api/move/running":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    app = _make_app(handler)
    async with app.router.lifespan_context(app):
        app.state.app_supervisor._apps["reachy_chat"].status = "running"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/expressions/play/dataset1/move1")
            assert response.status_code == 409
            assert "app" in response.json()["detail"].lower()
    assert play_posts == []  # never reached the daemon


@pytest.mark.asyncio
async def test_play_expression_409_while_app_loading() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    app = _make_app(handler)
    async with app.router.lifespan_context(app):
        app.state.app_supervisor._apps["reachy_dance"].status = "loading"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/expressions/play/dataset1/move1")
            assert response.status_code == 409


def _expr_session(phase: str = "playing") -> dict:
    return {"uuid": "u1" if phase == "playing" else None,
            "dataset": None, "move": None, "phase": phase}


@pytest.mark.asyncio
async def test_app_start_refused_while_expression_playing() -> None:
    app = _make_app(lambda request: httpx.Response(404))
    async with app.router.lifespan_context(app):
        app.state.expression_player._session = _expr_session("playing")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            with patch.object(AppSupervisor, "start_app", new=AsyncMock()) as start:
                response = await client.post("/api/apps/start/reachy_chat")
            assert response.status_code == 200  # existing refusal shape, not 409
            body = response.json()
            assert body["status"] == "refused"
            assert body["name"] == "reachy_chat"
            assert "expression" in body["error"].lower()
            start.assert_not_awaited()  # refused before delegation


@pytest.mark.asyncio
async def test_app_start_refused_while_returning_home() -> None:
    """Pins #67 auto-blocking: a return-home session also refuses app starts."""
    app = _make_app(lambda request: httpx.Response(404))
    async with app.router.lifespan_context(app):
        app.state.expression_player._session = _expr_session("returning_home")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/apps/start/reachy_chat")
            assert response.json()["status"] == "refused"


@pytest.mark.asyncio
async def test_app_start_allowed_when_expression_idle() -> None:
    """The guard must not over-block: no session -> delegates to the supervisor."""
    app = _make_app(lambda request: httpx.Response(404))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            with patch.object(
                AppSupervisor,
                "start_app",
                new=AsyncMock(return_value={"name": "reachy_dance", "status": "loading", "pid": 1}),
            ) as start:
                response = await client.post("/api/apps/start/reachy_dance")
            assert response.json()["status"] == "loading"
            start.assert_awaited_once_with("reachy_dance")


@pytest.mark.asyncio
async def test_motor_enable_route_kicks_return_home() -> None:
    goto_posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/motors/set_mode/enabled":
            return httpx.Response(200, json={"mode": "enabled"})
        if path == "/api/move/goto":
            goto_posts.append(path)
            return httpx.Response(200, json={"uuid": "home-uuid"})
        if path == "/api/move/running":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    app = _make_app(handler)
    async with app.router.lifespan_context(app):
        app.state.expression_player.POLL_INTERVAL_S = 0.01
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/motors/set_mode/enabled")
            assert response.status_code == 200
            assert response.json() == {"mode": "enabled"}  # proxy body unmodified

            # Session is live synchronously with the route return.
            current = await client.get("/api/expressions/current")
            assert current.json()["active"] is True
            assert current.json()["phase"] == "returning_home"

            await app.state.expression_player._watcher
            assert goto_posts  # daemon saw POST /api/move/goto


@pytest.mark.asyncio
async def test_motor_disable_route_sets_needs_home() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/motors/set_mode/disabled":
            return httpx.Response(200, json={"mode": "disabled"})
        if request.url.path == "/api/move/running":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    app = _make_app(handler)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/api/motors/set_mode/disabled")
            response = await client.post("/api/expressions/play/dataset1/move1")
            assert response.status_code == 409
            assert "enabled" in response.json()["detail"]


@pytest.mark.asyncio
async def test_play_blocked_during_post_enable_return_home() -> None:
    goto_posted = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal goto_posted
        path = request.url.path
        if path == "/api/motors/set_mode/enabled":
            return httpx.Response(200, json={"mode": "enabled"})
        if path == "/api/move/goto":
            goto_posted = True
            return httpx.Response(200, json={"uuid": "home-uuid"})
        if path == "/api/move/running":
            # Only report the goto in flight once it was actually dispatched —
            # otherwise the play()'s foreign-move check would 409 on its own and
            # this test could pass with the enable→return_home wiring deleted.
            return httpx.Response(200, json=[{"uuid": "home-uuid"}] if goto_posted else [])
        return httpx.Response(404)

    app = _make_app(handler)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/api/motors/set_mode/enabled")
            response = await client.post("/api/expressions/play/dataset1/move1")
            assert response.status_code == 409
            # Session-busy detail — distinguishes the live return-home session
            # from the needs_home refusal ("Robot pose is not homed — ...").
            assert response.json()["detail"] == "Robot is busy with another move."
    # lifespan aclose() cancels the still-polling watcher — no leak
