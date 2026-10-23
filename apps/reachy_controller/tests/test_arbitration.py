"""Cross-surface arbitration: apps <-> expressions (#66) + motor-enable return-home (#67).

Apps now run inside the daemon, so "an app is active" is read from the daemon's
``/api/apps/current-app-status``.
"""

from __future__ import annotations

import httpx
import pytest

from reachy_controller.daemon_proxy import DaemonProxy
from reachy_controller.main import create_app
from reachy_controller.routes.expressions import ExpressionPlayer

INSTALLED = [
    {"name": "reachy_mini_radio", "extra": {"custom_app_url": "http://0.0.0.0:8042"}},
    {"name": "hello_world", "extra": {"custom_app_url": None}},
]


def _make_app(handler):
    proxy = DaemonProxy("http://daemon.local:8000", transport=httpx.MockTransport(handler))
    return create_app(proxy)


def _apps_handler(current=None, start_posts=None, fallback=None):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/apps/current-app-status":
            return httpx.Response(200, json=current)
        if path == "/api/apps/list-available/installed":
            return httpx.Response(200, json=INSTALLED)
        if path == "/api/daemon/status":
            return httpx.Response(
                200, json={"state": "running", "backend_status": {"motor_control_mode": "enabled"}}
            )
        if path.startswith("/api/apps/start-app/"):
            if start_posts is not None:
                start_posts.append(path)
            name = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"info": {"name": name}, "state": "starting"})
        if path == "/api/move/running" and fallback is None:
            return httpx.Response(200, json=[])
        if fallback is not None:
            return fallback(request)
        return httpx.Response(404)

    return handler


def _running(name: str = "reachy_mini_radio", state: str = "running") -> dict:
    return {"info": {"name": name}, "state": state, "error": None}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (None, False),
        (_running(state="starting"), True),
        (_running(state="running"), True),
        (_running(state="stopping"), False),
        (_running(state="error"), False),
        (_running(state="done"), False),
    ],
)
async def test_any_app_active_states(current, expected) -> None:
    app = _make_app(_apps_handler(current=current))
    async with app.router.lifespan_context(app):
        assert await app.state.daemon_apps.refresh_active() is expected


@pytest.mark.asyncio
async def test_unreachable_daemon_counts_as_active() -> None:
    """Fail closed: if we can't see the app state, don't actuate."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    app = _make_app(handler)
    async with app.router.lifespan_context(app):
        assert await app.state.daemon_apps.refresh_active() is True


def test_expression_player_active_property() -> None:
    from unittest.mock import AsyncMock

    player = ExpressionPlayer(AsyncMock())
    assert player.active is False
    player._session = {"uuid": None, "dataset": None, "move": None, "phase": "returning_home"}
    assert player.active is True


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "starting"])
async def test_play_expression_409_while_app_active(state: str) -> None:
    play_posts: list[str] = []

    def fallback(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/move/play/"):
            play_posts.append(request.url.path)
            return httpx.Response(200, json={"uuid": "u1"})
        if request.url.path == "/api/move/running":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    app = _make_app(_apps_handler(current=_running(state=state), fallback=fallback))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/expressions/play/dataset1/move1")
            assert response.status_code == 409
            assert "app" in response.json()["detail"].lower()
    assert play_posts == []  # never reached the daemon


def _expr_session(phase: str = "playing") -> dict:
    return {"uuid": "u1" if phase == "playing" else None,
            "dataset": None, "move": None, "phase": phase}


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["playing", "returning_home"])
async def test_app_start_refused_while_expression_session(phase: str) -> None:
    start_posts: list[str] = []
    app = _make_app(_apps_handler(start_posts=start_posts))
    async with app.router.lifespan_context(app):
        app.state.expression_player._session = _expr_session(phase)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/apps/start/reachy_mini_radio")
            assert response.status_code == 200  # existing refusal shape, not 409
            body = response.json()
            assert body["status"] == "refused"
            assert body["name"] == "reachy_mini_radio"
            assert "expression" in body["error"].lower()
    assert start_posts == []  # refused before reaching the daemon


@pytest.mark.asyncio
async def test_app_start_allowed_when_expression_idle() -> None:
    """The guard must not over-block: no session -> delegates to the daemon."""
    start_posts: list[str] = []
    app = _make_app(_apps_handler(start_posts=start_posts))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/apps/start/reachy_mini_radio")
            body = response.json()
            assert body["status"] == "loading"
            assert body["ui_port"] == 8042
    assert start_posts == ["/api/apps/start-app/reachy_mini_radio"]


@pytest.mark.asyncio
async def test_app_start_unknown_app_is_404() -> None:
    start_posts: list[str] = []
    app = _make_app(_apps_handler(start_posts=start_posts))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/apps/start/not_installed")
            assert response.status_code == 404
    assert start_posts == []


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

    app = _make_app(_apps_handler(fallback=handler))
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

    app = _make_app(_apps_handler(fallback=handler))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/api/motors/set_mode/disabled")
            response = await client.post("/api/expressions/play/dataset1/move1")
            assert response.status_code == 409
            assert "enabled" in response.json()["detail"].lower()


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

    app = _make_app(_apps_handler(fallback=handler))
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
