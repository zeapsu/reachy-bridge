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
