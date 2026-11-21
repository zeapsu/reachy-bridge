"""The idle reset state route (#292), against a backend loop in its own thread."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI

from reachy_bridge_runtime import main, upstream
from reachy_bridge_runtime.features import idle_reset


@pytest.fixture(autouse=True)
def _installers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upstream, "_app_installers", [])


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


class Backend:
    """The two attributes the feature reads, scheduled the way upstream does."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None, reset_s: float = 0.3) -> None:
        self._log_loop = loop
        self._idle_reset_task: asyncio.Task[None] | None = None
        self.reset_s = reset_s

    def request_idle_reset(self) -> None:
        assert self._log_loop is not None
        self._log_loop.call_soon_threadsafe(self._maybe_start_idle_reset)

    def _maybe_start_idle_reset(self) -> None:
        self._idle_reset_task = asyncio.create_task(asyncio.sleep(self.reset_s))


def _app(monkeypatch: pytest.MonkeyPatch, backend: object | None) -> FastAPI:
    daemon_main = types.ModuleType("reachy_mini.daemon.app.main")

    def create_app(*_args, **_kwargs) -> FastAPI:
        app = FastAPI()
        app.state.daemon = types.SimpleNamespace(backend=backend)
        return app

    daemon_main.create_app = create_app
    package = types.ModuleType("reachy_mini.daemon.app")
    package.main = daemon_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "reachy_mini.daemon.app", package)
    monkeypatch.setitem(sys.modules, "reachy_mini.daemon.app.main", daemon_main)
    idle_reset.apply()
    return daemon_main.create_app()


async def _pending(app: FastAPI) -> bool:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://d") as client:
        response = await client.get("/api/bridge/idle-reset")
    assert response.status_code == 200
    return response.json()["pending"]


@pytest.mark.asyncio
async def test_pending_from_the_request_until_the_reset_ends(
    monkeypatch: pytest.MonkeyPatch, loop: asyncio.AbstractEventLoop
) -> None:
    backend = Backend(loop)
    app = _app(monkeypatch, backend)
    assert await _pending(app) is False
    # Read straight after the request, before the loop has created the task:
    # the read queues behind the scheduling, so it still sees the reset.
    backend.request_idle_reset()
    assert await _pending(app) is True
    deadline = time.monotonic() + 5
    while await _pending(app):
        assert time.monotonic() < deadline
        await asyncio.sleep(0.05)
    assert backend._idle_reset_task is not None and backend._idle_reset_task.done()


@pytest.mark.asyncio
async def test_no_backend_or_no_loop_is_not_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _pending(_app(monkeypatch, None)) is False
    assert await _pending(_app(monkeypatch, Backend(None))) is False


@pytest.mark.asyncio
async def test_a_loop_that_does_not_answer_reads_as_pending(
    monkeypatch: pytest.MonkeyPatch, loop: asyncio.AbstractEventLoop
) -> None:
    monkeypatch.setattr(idle_reset, "READ_TIMEOUT_S", 0.1)
    app = _app(monkeypatch, Backend(loop))
    blocked = threading.Event()
    loop.call_soon_threadsafe(lambda: blocked.wait(2))
    try:
        assert await _pending(app) is True
    finally:
        blocked.set()


def test_it_is_a_core_route_feature() -> None:
    assert idle_reset.FEATURE in main.CORE_FEATURES
    assert idle_reset.FEATURE in main.ROUTE_FEATURES


def test_the_installed_daemon_matches() -> None:
    assert upstream.check({idle_reset.FEATURE}) == {}
