"""Kinematics engine route and saved-engine startup in the daemon wrapper."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).parent))

import reachy_mini_daemon_jetson as jetson  # noqa: E402


@pytest.fixture(autouse=True)
def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("REACHY_BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(jetson, "_placo_available", lambda: True)
    return tmp_path


def _app(monkeypatch: pytest.MonkeyPatch, *, backend: object | None = None) -> FastAPI:
    daemon_main = types.ModuleType("reachy_mini.daemon.app.main")

    def create_app(*_args, **_kwargs) -> FastAPI:
        app = FastAPI()
        app.state.args = types.SimpleNamespace(kinematics_engine="AnalyticalKinematics")
        app.state.daemon = types.SimpleNamespace(backend=backend)
        return app

    daemon_main.create_app = create_app
    package = types.ModuleType("reachy_mini.daemon.app")
    package.main = daemon_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "reachy_mini.daemon.app", package)
    monkeypatch.setitem(sys.modules, "reachy_mini.daemon.app.main", daemon_main)
    jetson._install_kinematics_route()
    return daemon_main.create_app()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://d")


@pytest.mark.asyncio
async def test_switch_while_off_sets_next_engine_and_saves_it(
    monkeypatch: pytest.MonkeyPatch, _state: Path
) -> None:
    app = _app(monkeypatch)
    async with _client(app) as client:
        response = await client.put("/api/bridge/kinematics", json={"engine": "Placo"})
    assert response.json()["engine"] == "Placo"
    assert app.state.args.kinematics_engine == "Placo"
    assert json.loads((_state / "kinematics.json").read_text()) == {"engine": "Placo"}


@pytest.mark.asyncio
async def test_switch_is_refused_while_the_backend_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = types.SimpleNamespace(kinematics_engine="AnalyticalKinematics")
    app = _app(monkeypatch, backend=backend)
    async with _client(app) as client:
        response = await client.put("/api/bridge/kinematics", json={"engine": "Placo"})
        status = (await client.get("/api/bridge/kinematics")).json()
    assert response.status_code == 409
    assert status["active"] == "AnalyticalKinematics"
    assert app.state.args.kinematics_engine == "AnalyticalKinematics"


@pytest.mark.asyncio
async def test_unknown_or_missing_engine_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jetson, "_placo_available", lambda: False)
    app = _app(monkeypatch)
    async with _client(app) as client:
        assert (await client.put("/api/bridge/kinematics", json={"engine": "NN"})).status_code == 400
        assert (await client.put("/api/bridge/kinematics", json={"engine": "Placo"})).status_code == 400
        available = (await client.get("/api/bridge/kinematics")).json()["available"]
    assert available == ["AnalyticalKinematics"]


def test_saved_engine_is_passed_to_the_daemon(monkeypatch: pytest.MonkeyPatch, _state: Path) -> None:
    (_state / "kinematics.json").write_text(json.dumps({"engine": "Placo"}))
    monkeypatch.setattr(sys, "argv", ["wrapper", "--headless"])
    jetson._apply_saved_kinematics_engine()
    assert sys.argv == ["wrapper", "--headless", "--kinematics-engine", "Placo"]


def test_command_line_engine_wins_over_the_saved_one(
    monkeypatch: pytest.MonkeyPatch, _state: Path
) -> None:
    (_state / "kinematics.json").write_text(json.dumps({"engine": "Placo"}))
    monkeypatch.setattr(sys, "argv", ["wrapper", "--kinematics-engine", "AnalyticalKinematics"])
    jetson._apply_saved_kinematics_engine()
    assert sys.argv == ["wrapper", "--kinematics-engine", "AnalyticalKinematics"]


@pytest.mark.asyncio
async def test_restart_keeps_the_engine_of_the_last_start(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    class FakeDaemon:
        async def start(self, **kwargs) -> str:
            calls.append(kwargs)
            return "running"

        async def restart(self) -> str:
            # Like Daemon.restart in 1.12.0: start() without kinematics_engine.
            return await self.start(sim=False)

    daemon_module = types.ModuleType("reachy_mini.daemon.daemon")
    daemon_module.Daemon = FakeDaemon  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "reachy_mini.daemon.daemon", daemon_module)
    jetson._apply_restart_keeps_kinematics_patch()

    daemon = FakeDaemon()
    await daemon.start(sim=False, kinematics_engine="Placo")
    await daemon.restart()
    assert [call.get("kinematics_engine") for call in calls] == ["Placo", "Placo"]
