"""Media back to the daemon after an app that released it exits."""

from __future__ import annotations

import asyncio
import types
from typing import Any

from reachy_mini.daemon.daemon import DaemonState

from reachy_bridge_runtime.features import media_return


class Daemon:
    def __init__(self, *, released: bool, running: bool = True, fail: bool = False) -> None:
        self._media_released = released
        self._status = types.SimpleNamespace(media_released=released, state=DaemonState.RUNNING if running else DaemonState.STOPPED)
        self._media_server = object()
        self.fail = fail
        self.acquired = 0

    @property
    def media_released(self) -> bool:
        return self._media_released

    def status(self) -> Any:
        return self._status

    async def acquire_media(self) -> None:
        if self.fail:
            raise RuntimeError("pipeline would not start")
        self.acquired += 1
        self._media_released = False
        self._status.media_released = False


def run_app(daemon: Daemon, *, release: bool) -> types.SimpleNamespace:
    """Start an app through the wrapper; it may release media, then exits."""
    manager = types.SimpleNamespace(daemon=daemon, current_app=None)

    async def scenario() -> None:
        exit_app = asyncio.Event()

        async def monitor() -> None:
            if release:
                daemon._media_released = True
            await exit_app.wait()

        async def original(manager: Any, app_name: str) -> str:
            manager.current_app = types.SimpleNamespace(monitor_task=asyncio.create_task(monitor()))
            return "starting"

        assert await media_return._start_app(original, manager, "app") == "starting"
        await asyncio.sleep(0)
        exit_app.set()
        await manager.current_app.monitor_task
        for _ in range(3):
            await asyncio.sleep(0)

    asyncio.run(scenario())
    return manager


def test_a_running_daemon_gets_the_media_back_when_the_app_exits() -> None:
    daemon = Daemon(released=False)
    run_app(daemon, release=True)
    assert daemon.acquired == 1
    assert not daemon.media_released


def test_an_app_that_kept_the_daemons_media_changes_nothing() -> None:
    daemon = Daemon(released=False)
    run_app(daemon, release=False)
    assert daemon.acquired == 0


def test_a_stopped_daemon_forgets_the_release_for_its_next_start() -> None:
    daemon = Daemon(released=False, running=False)
    run_app(daemon, release=True)
    assert daemon.acquired == 0
    assert not daemon._media_released
    assert not daemon._status.media_released


def test_a_newer_running_app_keeps_its_release() -> None:
    daemon = Daemon(released=True)

    async def scenario() -> bool:
        old = asyncio.get_running_loop().create_future()
        old.set_result(None)
        newer = asyncio.create_task(asyncio.sleep(1))
        manager = types.SimpleNamespace(daemon=daemon, current_app=types.SimpleNamespace(monitor_task=newer))
        taken = await media_return.take_back(manager, old, "old app")
        newer.cancel()
        return taken

    assert asyncio.run(scenario()) is False
    assert daemon.media_released


def test_a_failed_acquire_is_logged_not_raised(caplog: Any) -> None:
    daemon = Daemon(released=True, fail=True)
    manager = types.SimpleNamespace(daemon=daemon, current_app=None)
    assert asyncio.run(media_return.take_back(manager, None, "app")) is False
    assert "could not take them back" in caplog.text


def test_it_is_a_core_feature() -> None:
    from reachy_bridge_runtime import main

    assert media_return.FEATURE in main.CORE_FEATURES
    assert main.FEATURES[media_return.FEATURE] is media_return.apply
