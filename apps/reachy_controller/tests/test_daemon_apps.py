"""Robot power and app routes backed by the daemon API."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from reachy_controller.daemon_proxy import DaemonProxy
from reachy_controller.main import create_app

INSTALLED = [
    {"name": "reachy_mini_radio", "description": "", "extra": {"custom_app_url": "http://0.0.0.0:8042"}},
    {"name": "hello_world", "description": "", "extra": {"custom_app_url": None}},
]


class FakeDaemon:
    def __init__(
        self, *, state: str = "stopped", current: dict | None = None, motors: str = "enabled"
    ) -> None:
        self.state = state
        self.motors = motors
        self.current = current
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, f"{path}?{request.url.query.decode()}".rstrip("?")))
        if path == "/api/daemon/status":
            backend = {"motor_control_mode": self.motors} if self.state == "running" else None
            return httpx.Response(
                200, json={"state": self.state, "error": None, "backend_status": backend}
            )
        if path in ("/api/daemon/start", "/api/daemon/stop"):
            return httpx.Response(200, json={"job_id": "j1"})
        if path in ("/api/motors/set_mode/enabled", "/api/move/play/wake_up"):
            if path.endswith("wake_up"):
                self.motors = "enabled"
            return httpx.Response(200, json={"uuid": "u"})
        if path == "/api/move/running":
            return httpx.Response(200, json=[])
        if path.startswith("/api/apps/start-app/"):
            return httpx.Response(200, json={"state": "starting"})
        if path == "/api/apps/list-available/installed":
            return httpx.Response(200, json=INSTALLED)
        if path == "/api/apps/current-app-status":
            return httpx.Response(200, json=self.current)
        if path == "/api/apps/stop-current-app":
            self.current = None
            return httpx.Response(200, json=None)
        return httpx.Response(404)


async def _client(daemon: FakeDaemon):
    proxy = DaemonProxy("http://daemon.local:8000", transport=httpx.MockTransport(daemon))
    app = create_app(proxy)
    return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REACHY_BRIDGE_STATE_DIR", str(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "running", "status"),
    [("running", True, "running"), ("stopped", False, "stopped"), ("asleep", False, "asleep"),
     ("not_initialized", False, "not_initialized"), ("starting", False, "starting")],
)
async def test_status_maps_daemon_state(state: str, running: bool, status: str) -> None:
    daemon = FakeDaemon(state="running", motors="disabled") if state == "asleep" else FakeDaemon(
        state=state
    )
    app, client = await _client(daemon)
    async with client:
        body = (await client.get("/api/daemon/status")).json()
    assert body["reachable"] is True
    assert body["running"] is running
    assert body["status"] == status


@pytest.mark.asyncio
async def test_status_reports_unreachable_daemon() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    proxy = DaemonProxy("http://daemon.local:8000", transport=httpx.MockTransport(down))
    app = create_app(proxy)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        body = (await c.get("/api/daemon/status")).json()
    assert body == {"reachable": False, "running": False, "manageable": False, "status": "offline"}


@pytest.mark.asyncio
async def test_power_on_wakes_robot_and_clears_needs_home() -> None:
    daemon = FakeDaemon()
    app, client = await _client(daemon)
    app.state.expression_player.needs_home = True
    async with client:
        response = await client.post("/api/daemon/start")
    assert response.status_code == 200
    assert ("POST", "/api/daemon/start?wake_up=true") in daemon.calls
    assert app.state.expression_player.needs_home is False


@pytest.mark.asyncio
async def test_power_off_stops_running_app_then_sleeps() -> None:
    current = {"info": {"name": "hello_world"}, "state": "running"}
    daemon = FakeDaemon(state="running", current=current)
    app, client = await _client(daemon)
    async with client:
        await client.post("/api/daemon/stop")
    posts = [path for method, path in daemon.calls if method == "POST"]
    assert posts == ["/api/apps/stop-current-app", "/api/daemon/stop?goto_sleep=true"]
    assert app.state.expression_player.needs_home is True


@pytest.mark.asyncio
async def test_apps_status_lists_installed_with_current_state() -> None:
    daemon = FakeDaemon(current={"info": {"name": "reachy_mini_radio"}, "state": "starting"})
    app, client = await _client(daemon)
    async with client:
        body = (await client.get("/api/apps/status")).json()
    by_name = {item["name"]: item for item in body}
    assert by_name["reachy_mini_radio"]["status"] == "loading"
    assert by_name["reachy_mini_radio"]["has_ui"] is True
    assert by_name["reachy_mini_radio"]["display"] == "Radio"
    assert by_name["hello_world"]["status"] == "stopped"
    assert by_name["hello_world"]["has_ui"] is False


@pytest.mark.asyncio
async def test_app_ui_port_comes_from_custom_app_url() -> None:
    app, client = await _client(FakeDaemon())
    async with client:
        assert (await client.get("/api/apps/reachy_mini_radio/ui")).json()["port"] == 8042
        assert (await client.get("/api/apps/hello_world/ui")).json()["port"] is None


@pytest.mark.asyncio
async def test_favorites_default_persist_and_validate(tmp_path: Path) -> None:
    app, client = await _client(FakeDaemon())
    async with client:
        assert (await client.get("/api/apps/favorites")).json() == {
            "favorites": ["reachy_mini_radio", "hello_world"]
        }
        saved = await client.put("/api/apps/favorites", json={"favorites": ["hello_world"]})
        assert saved.json() == {"favorites": ["hello_world"]}
        assert (await client.get("/api/apps/favorites")).json() == {"favorites": ["hello_world"]}
        unknown = await client.put("/api/apps/favorites", json={"favorites": ["nope"]})
        assert unknown.status_code == 422
        too_many = await client.put("/api/apps/favorites", json={"favorites": ["a", "b", "c", "d"]})
        assert too_many.status_code == 422
    assert (tmp_path / "favorites.json").exists()


@pytest.mark.asyncio
async def test_power_on_while_asleep_wakes_in_place() -> None:
    daemon = FakeDaemon(state="running", motors="disabled")
    app, client = await _client(daemon)
    async with client:
        await client.post("/api/daemon/start")
    posts = [path for method, path in daemon.calls if method == "POST"]
    assert posts == ["/api/motors/set_mode/enabled", "/api/move/play/wake_up"]


@pytest.mark.asyncio
async def test_power_off_while_asleep_skips_sleep_move() -> None:
    daemon = FakeDaemon(state="running", motors="disabled")
    app, client = await _client(daemon)
    async with client:
        await client.post("/api/daemon/stop")
    posts = [path for method, path in daemon.calls if method == "POST"]
    assert posts == ["/api/daemon/stop?goto_sleep=false"]


@pytest.mark.asyncio
async def test_app_start_wakes_asleep_robot_first() -> None:
    daemon = FakeDaemon(state="running", motors="disabled")
    app, client = await _client(daemon)
    async with client:
        body = (await client.post("/api/apps/start/reachy_mini_radio")).json()
    assert body["status"] == "loading"
    posts = [path for method, path in daemon.calls if method == "POST"]
    assert posts == [
        "/api/motors/set_mode/enabled",
        "/api/move/play/wake_up",
        "/api/apps/start-app/reachy_mini_radio",
    ]


@pytest.mark.asyncio
async def test_app_start_on_awake_robot_does_not_move_it() -> None:
    daemon = FakeDaemon(state="running", motors="enabled")
    app, client = await _client(daemon)
    async with client:
        await client.post("/api/apps/start/reachy_mini_radio")
    posts = [path for method, path in daemon.calls if method == "POST"]
    assert posts == ["/api/apps/start-app/reachy_mini_radio"]


@pytest.mark.asyncio
async def test_app_ui_reports_not_ready_when_port_closed() -> None:
    app, client = await _client(FakeDaemon())
    async with client:
        body = (await client.get("/api/apps/reachy_mini_radio/ui")).json()
    assert body["port"] == 8042
    assert body["ready"] is False
