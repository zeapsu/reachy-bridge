"""Robot power and app routes backed by the daemon API."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import httpx
import pytest

from reachy_controller.daemon_apps import APP_CONTROL_TIMEOUT_S, DaemonApps
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
        self.timeouts: dict[str, float | None] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, f"{path}?{request.url.query.decode()}".rstrip("?")))
        self.timeouts[path] = request.extensions.get("timeout", {}).get("read")
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


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
async def test_app_ui_reports_not_ready_when_port_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    port = _closed_port()
    monkeypatch.setitem(INSTALLED[0], "extra", {"custom_app_url": f"http://0.0.0.0:{port}"})
    app, client = await _client(FakeDaemon())
    async with client:
        body = (await client.get("/api/apps/reachy_mini_radio/ui")).json()
    assert body["port"] == port
    assert body["ready"] is False


@pytest.mark.asyncio
async def test_app_ui_is_503_when_daemon_down() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    app, client = await _client(down)  # type: ignore[arg-type]
    async with client:
        response = await client.get("/api/apps/reachy_mini_radio/ui")
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_slow_app_calls_get_a_longer_timeout() -> None:
    # The daemon answers these only after the app spawns or exits, which takes
    # longer than the default 2 s request timeout.
    crashed = {"info": {"name": "hello_world"}, "state": "error", "error": "exit 1"}
    daemon = FakeDaemon(state="running", motors="enabled", current=crashed)
    app, client = await _client(daemon)
    async with client:
        await client.post("/api/apps/start/reachy_mini_radio")
        assert daemon.timeouts["/api/apps/stop-current-app"] == APP_CONTROL_TIMEOUT_S
        assert daemon.timeouts["/api/apps/start-app/reachy_mini_radio"] == APP_CONTROL_TIMEOUT_S
        del daemon.timeouts["/api/apps/stop-current-app"]
        await client.post("/api/apps/stop/reachy_mini_radio")
        assert daemon.timeouts["/api/apps/stop-current-app"] == APP_CONTROL_TIMEOUT_S
        assert daemon.timeouts["/api/daemon/status"] == 2.0


@pytest.mark.asyncio
async def test_power_off_waits_for_the_app_stop() -> None:
    running = {"info": {"name": "reachy_mini_radio"}, "state": "running", "error": None}
    daemon = FakeDaemon(state="running", motors="enabled", current=running)
    app, client = await _client(daemon)
    async with client:
        await client.post("/api/daemon/stop")
    assert daemon.timeouts["/api/apps/stop-current-app"] == APP_CONTROL_TIMEOUT_S


@pytest.mark.asyncio
async def test_uptime_restarts_when_the_same_app_starts_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import reachy_controller.daemon_apps as daemon_apps

    clock = [100.0]
    monkeypatch.setattr(daemon_apps.time, "time", lambda: clock[0])
    daemon = FakeDaemon(state="running", motors="enabled")
    proxy = DaemonProxy("http://daemon.local:8000", transport=httpx.MockTransport(daemon))
    apps = DaemonApps(proxy)

    def app_state(state: str) -> dict:
        return {"info": {"name": "reachy_mini_radio"}, "state": state, "error": None}

    daemon.current = app_state("running")
    await apps.current()
    clock[0] = 200.0
    daemon.current = app_state("done")
    await apps.current()
    daemon.current = app_state("running")
    await apps.current()
    radio = next(s for s in await apps.statuses() if s["name"] == "reachy_mini_radio")
    assert radio["start_time"] == 200.0
    await proxy.close()


@pytest.mark.asyncio
async def test_app_start_clears_crashed_app_first() -> None:
    crashed = {"info": {"name": "hello_world"}, "state": "error", "error": "exit 1"}
    daemon = FakeDaemon(state="running", motors="enabled", current=crashed)
    app, client = await _client(daemon)
    async with client:
        body = (await client.post("/api/apps/start/reachy_mini_radio")).json()
    assert body["status"] == "loading"
    posts = [path for method, path in daemon.calls if method == "POST"]
    assert posts == ["/api/apps/stop-current-app", "/api/apps/start-app/reachy_mini_radio"]


class SlowFakeDaemon(FakeDaemon):
    """Yields on every request and finishes a power-on later, like the daemon's job."""

    async def __call__(self, request: httpx.Request) -> httpx.Response:  # type: ignore[override]
        await asyncio.sleep(0.01)
        if request.url.path == "/api/daemon/start" and self.state == "stopped":
            self.state = "starting"

            async def finish() -> None:
                await asyncio.sleep(0.1)
                self.state = "running"

            asyncio.get_running_loop().create_task(finish())
        return super().__call__(request)


@pytest.mark.asyncio
async def test_concurrent_power_on_from_asleep_wakes_once() -> None:
    daemon = SlowFakeDaemon(state="running", motors="disabled")
    app, client = await _client(daemon)  # type: ignore[arg-type]
    async with client:
        await asyncio.gather(client.post("/api/daemon/start"), client.post("/api/daemon/start"))
    posts = [path for method, path in daemon.calls if method == "POST"]
    assert posts == ["/api/motors/set_mode/enabled", "/api/move/play/wake_up"]


@pytest.mark.asyncio
async def test_app_start_during_power_on_waits_instead_of_starting_again() -> None:
    daemon = SlowFakeDaemon(state="stopped")
    app, client = await _client(daemon)  # type: ignore[arg-type]
    async with client:
        _, started = await asyncio.gather(
            client.post("/api/daemon/start"),
            client.post("/api/apps/start/reachy_mini_radio"),
        )
    assert started.json()["status"] == "loading"
    posts = [path for method, path in daemon.calls if method == "POST"]
    assert posts == ["/api/daemon/start?wake_up=true", "/api/apps/start-app/reachy_mini_radio"]


class KinematicsFakeDaemon(FakeDaemon):
    """Power on/off take effect at once; the bridge route behaves like the wrapper's."""

    def __init__(
        self,
        *,
        engine: str = "AnalyticalKinematics",
        refuse: bool = False,
        unreachable_on_put: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.engine = engine
        self.refuse = refuse
        self.unreachable_on_put = unreachable_on_put

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/bridge/kinematics":
            self.calls.append((request.method, path))
            if self.unreachable_on_put and request.method == "PUT":
                raise httpx.ConnectError("refused", request=request)
            if request.method == "PUT":
                if self.state == "running":
                    return httpx.Response(409, json={"detail": "Turn the robot off"})
                if self.refuse:
                    return httpx.Response(400, json={"detail": "Placo kinematics is not installed"})
                self.engine = json.loads(request.content)["engine"]
            active = self.engine if self.state == "running" else None
            return httpx.Response(
                200, json={"engine": self.engine, "active": active, "available": [self.engine]}
            )
        response = super().__call__(request)
        if path == "/api/daemon/stop":
            self.state = "stopped"
        elif path == "/api/daemon/start":
            woke = request.url.params.get("wake_up") == "true"
            self.state, self.motors = "running", "enabled" if woke else "disabled"
        return response


def _power_and_switch_calls(daemon: FakeDaemon) -> list[str]:
    return [
        path for method, path in daemon.calls
        if method in ("POST", "PUT") and not path.startswith("/api/apps/")
    ]


@pytest.mark.asyncio
async def test_kinematics_switch_while_off_does_not_move_the_robot() -> None:
    daemon = KinematicsFakeDaemon(state="stopped")
    app, client = await _client(daemon)
    async with client:
        body = (await client.put("/api/daemon/kinematics", json={"engine": "Placo"})).json()
    assert (body["engine"], body["restarted"]) == ("Placo", False)
    assert _power_and_switch_calls(daemon) == ["/api/bridge/kinematics"]


@pytest.mark.asyncio
async def test_kinematics_switch_while_on_powers_off_switches_and_powers_on() -> None:
    daemon = KinematicsFakeDaemon(state="running", motors="enabled")
    app, client = await _client(daemon)
    async with client:
        body = (await client.put("/api/daemon/kinematics", json={"engine": "Placo"})).json()
    assert (body["engine"], body["restarted"]) == ("Placo", True)
    assert _power_and_switch_calls(daemon) == [
        "/api/daemon/stop?goto_sleep=true",
        "/api/bridge/kinematics",
        "/api/daemon/start?wake_up=true",
    ]


@pytest.mark.asyncio
async def test_kinematics_switch_is_refused_while_an_app_runs() -> None:
    running = {"info": {"name": "reachy_mini_radio"}, "state": "running", "error": None}
    daemon = KinematicsFakeDaemon(state="running", motors="enabled", current=running)
    app, client = await _client(daemon)
    async with client:
        response = await client.put("/api/daemon/kinematics", json={"engine": "Placo"})
    assert response.status_code == 409
    assert _power_and_switch_calls(daemon) == []


@pytest.mark.asyncio
async def test_failed_kinematics_switch_powers_the_robot_back_on() -> None:
    daemon = KinematicsFakeDaemon(state="running", motors="enabled", refuse=True)
    app, client = await _client(daemon)
    async with client:
        response = await client.put("/api/daemon/kinematics", json={"engine": "Placo"})
    assert response.status_code == 400
    assert daemon.state == "running"
    assert daemon.engine == "AnalyticalKinematics"


@pytest.mark.asyncio
async def test_kinematics_switch_while_asleep_leaves_the_robot_asleep() -> None:
    daemon = KinematicsFakeDaemon(state="running", motors="disabled")
    app, client = await _client(daemon)
    async with client:
        body = (await client.put("/api/daemon/kinematics", json={"engine": "Placo"})).json()
    assert body["restarted"] is True
    assert _power_and_switch_calls(daemon) == [
        "/api/daemon/stop?goto_sleep=false",
        "/api/bridge/kinematics",
        "/api/daemon/start?wake_up=false",
    ]
    assert (daemon.state, daemon.motors) == ("running", "disabled")


@pytest.mark.asyncio
async def test_kinematics_switch_powers_off_an_errored_robot_whose_backend_is_up() -> None:
    class ErroredDaemon(KinematicsFakeDaemon):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/daemon/status" and self.state == "error":
                self.calls.append(("GET", "/api/daemon/status"))
                return httpx.Response(200, json={
                    "state": "error", "error": "wake failed",
                    "backend_status": {"motor_control_mode": "enabled"},
                })
            return super().__call__(request)

    daemon = ErroredDaemon(state="error")
    app, client = await _client(daemon)
    async with client:
        response = await client.put("/api/daemon/kinematics", json={"engine": "Placo"})
    assert response.status_code == 200
    assert _power_and_switch_calls(daemon)[:2] == [
        "/api/daemon/stop?goto_sleep=true", "/api/bridge/kinematics"
    ]


@pytest.mark.asyncio
async def test_unreachable_daemon_during_switch_still_powers_back_on() -> None:
    daemon = KinematicsFakeDaemon(state="running", motors="enabled", unreachable_on_put=True)
    app, client = await _client(daemon)
    async with client:
        response = await client.put("/api/daemon/kinematics", json={"engine": "Placo"})
    assert response.status_code == 503
    assert _power_and_switch_calls(daemon)[-1] == "/api/daemon/start?wake_up=true"


@pytest.mark.asyncio
async def test_app_start_leaves_gravity_compensation_first() -> None:
    daemon = FakeDaemon(state="running", motors="gravity_compensation")
    app, client = await _client(daemon)
    async with client:
        body = (await client.post("/api/apps/start/reachy_mini_radio")).json()
    assert body["status"] == "loading"
    posts = [path for method, path in daemon.calls if method == "POST"]
    assert posts == ["/api/motors/set_mode/enabled", "/api/apps/start-app/reachy_mini_radio"]
