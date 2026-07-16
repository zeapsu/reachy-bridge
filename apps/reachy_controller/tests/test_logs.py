"""Focused bounded log broker, pump, handler, and source route tests."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from reachy_controller.log_stream import ControllerLogHandler, LogBroker, ProcessLogPump
import reachy_controller.log_stream as log_stream_module

from reachy_controller.main import create_app
from reachy_controller.routes.logs import _stream_events, log_stream


@pytest.mark.asyncio
async def test_broker_ring_replay_and_independent_bounded_queues() -> None:
    broker = LogBroker(ring_size=5, queue_size=3)
    broker.bind_source("x")
    first = await broker.subscribe("x")
    second = await broker.subscribe("x")
    await asyncio.to_thread(lambda: [broker.publish("x", str(i)) for i in range(20)])
    await asyncio.sleep(0)
    assert broker.ring("x") == [str(i) for i in range(15, 20)]
    assert first._queue.qsize() <= 3
    assert second._queue.qsize() <= 3
    await first.aclose()
    await second.aclose()
    await broker.aclose()


@pytest.mark.asyncio
async def test_process_pump_merges_and_flushes_burst(tmp_path: Path) -> None:
    broker = LogBroker()
    broker.bind_source("burst")
    process = subprocess.Popen(
        [
            "python",
            "-c",
            "import sys; print('stdout'); print('stderr', file=sys.stderr)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    pump = ProcessLogPump(process, "burst", broker, tmp_path / "burst.log")
    await asyncio.to_thread(process.wait)
    await asyncio.to_thread(pump.stop, 5.0, True)
    assert (tmp_path / "burst.log").read_text().splitlines() == ["stdout", "stderr"]
    assert broker.ring("burst") == ["stdout", "stderr"]
    assert pump.thread is not None and not pump.thread.is_alive()
    await broker.aclose()


@pytest.mark.asyncio
async def test_process_pump_drains_unread_exit_tail_before_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exited child’s unread merged pipe tail is retained before pump close."""
    broker = LogBroker()
    broker.bind_source("exit-tail")
    select_entered = threading.Event()
    release_select = threading.Event()
    original_select = log_stream_module.select.select

    def paused_select(*args: object) -> tuple[list[int], list[int], list[int]]:
        select_entered.set()
        release_select.wait(timeout=1.0)
        return original_select(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(log_stream_module.select, "select", paused_select)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; print('stdout-tail', flush=True); print('stderr-tail', file=sys.stderr, flush=True)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    pump = ProcessLogPump(process, "exit-tail", broker, tmp_path / "exit-tail.log")
    try:
        assert await asyncio.to_thread(select_entered.wait, 1.0)
        await asyncio.to_thread(process.wait)
        stop_task = asyncio.create_task(asyncio.to_thread(pump.stop, 1.0, True))
        await asyncio.sleep(0.01)
        assert not stop_task.done()
        release_select.set()
        await stop_task
        lines = (tmp_path / "exit-tail.log").read_text().splitlines()
        assert lines == ["stdout-tail", "stderr-tail"]
        assert broker.ring("exit-tail") == lines
        assert pump.thread is not None and not pump.thread.is_alive()
    finally:
        release_select.set()
        if pump.thread is not None:
            pump.thread.join(timeout=1.0)
        if process.poll() is None:
            process.kill()
            process.wait()
        await broker.aclose()


@pytest.mark.asyncio
async def test_process_pump_stop_does_not_wait_for_inherited_pipe(tmp_path: Path) -> None:
    """A descendant holding stdout must not keep the pump thread alive."""
    broker = LogBroker()
    broker.bind_source("inherited")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                "print('partial', end='', flush=True)"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    pump = ProcessLogPump(process, "inherited", broker, tmp_path / "inherited.log")
    try:
        await asyncio.to_thread(process.wait)
        started = time.monotonic()
        await asyncio.to_thread(pump.stop, 0.5, True)
        elapsed = time.monotonic() - started
        assert elapsed < 1.0
        assert pump.thread is not None and not pump.thread.is_alive()
        assert (tmp_path / "inherited.log").read_text() == "partial\n"
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        await broker.aclose()


@pytest.mark.asyncio
async def test_process_pump_forced_join_releases_reader_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The forced second join waits for the reader before ownership is released."""
    broker = LogBroker()
    broker.bind_source("forced-join")
    select_entered = threading.Event()
    release_select = threading.Event()
    original_select = log_stream_module.select.select

    def paused_select(*args: object) -> tuple[list[int], list[int], list[int]]:
        select_entered.set()
        release_select.wait(timeout=1.0)
        return original_select(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(log_stream_module.select, "select", paused_select)
    process = subprocess.Popen(
        [sys.executable, "-c", "print('forced-tail', flush=True)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    pump = ProcessLogPump(process, "forced-join", broker, tmp_path / "forced-join.log")
    thread = pump.thread
    assert thread is not None
    real_join = thread.join
    join_timeouts: list[float | None] = []
    second_join_entered = threading.Event()

    def first_join(timeout: float | None = None) -> None:
        join_timeouts.append(timeout)
        if len(join_timeouts) > 1:
            second_join_entered.set()
            real_join(timeout)

    thread.join = first_join  # type: ignore[method-assign]
    try:
        assert await asyncio.to_thread(select_entered.wait, 1.0)
        await asyncio.to_thread(process.wait)
        stop_task = asyncio.create_task(asyncio.to_thread(pump.stop, 0.05, True))
        assert await asyncio.to_thread(second_join_entered.wait, 1.0)
        assert len(join_timeouts) == 2
        assert join_timeouts[0] == 0.05
        assert join_timeouts[1] <= ProcessLogPump.EXIT_DRAIN_MAX_S
        assert not stop_task.done()
        release_select.set()
        await stop_task
        assert not thread.is_alive()
        assert pump.file_handle is None
    finally:
        release_select.set()
        if thread.is_alive():
            thread.join(timeout=1.0)
        if process.poll() is None:
            process.kill()
            process.wait()
        await broker.aclose()


def _stream_request(app: object) -> object:
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/logs/stream",
            "query_string": b"source=reachy_chat",
            "headers": [],
            "scheme": "http",
            "app": app,
        }
    )


@pytest.mark.asyncio
async def test_stream_keeps_lines_published_during_persisted_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app()
    broker = app.state.log_broker
    path = tmp_path / "reachy_chat.log"
    path.write_text("persisted\n", encoding="utf-8")
    broker.set_file("reachy_chat", path)
    broker.reset("reachy_chat", status="empty")

    def delayed_tail(_: Path, __: int, ___: int | None = None) -> list[str]:
        broker.publish("reachy_chat", "during-tail")
        time.sleep(0.02)
        return ["persisted"]

    monkeypatch.setattr(broker, "tail_file", delayed_tail)
    response = await log_stream(_stream_request(app), "reachy_chat")  # type: ignore[arg-type]
    stream = response.body_iterator
    reset = await stream.__anext__()
    assert json.loads(reset.split("data: ", 1)[1]) == {
        "status": "stopped",
        "lines": ["persisted"],
    }
    status = await stream.__anext__()
    lines = await stream.__anext__()
    assert status.startswith("event: source-status")
    assert json.loads(lines.split("data: ", 1)[1]) == {"lines": ["during-tail"]}
    await stream.aclose()
    await broker.aclose()


@pytest.mark.asyncio
async def test_stream_reset_status_matches_persisted_source_status(tmp_path: Path) -> None:
    app = create_app()
    broker = app.state.log_broker
    path = tmp_path / "reachy_chat.log"
    path.write_text("persisted\n", encoding="utf-8")
    broker.set_file("reachy_chat", path)
    broker.reset("reachy_chat", status="empty")

    response = await log_stream(_stream_request(app), "reachy_chat")  # type: ignore[arg-type]
    reset = await response.body_iterator.__anext__()
    payload = json.loads(reset.split("data: ", 1)[1])
    assert payload["status"] == "stopped"
    assert payload["lines"] == ["persisted"]
    await response.body_iterator.aclose()
    await broker.aclose()


def test_controller_handler_formats_and_persists(tmp_path: Path) -> None:
    broker = LogBroker()
    handler = ControllerLogHandler(broker, tmp_path / "controller.log")
    record = logging.LogRecord("test", logging.WARNING, __file__, 1, "unsafe <line>", (), None)
    handler.emit(record)
    handler.close()
    assert broker.ring("controller")[0].endswith("WARNING  test: unsafe <line>")
    assert "unsafe <line>" in (tmp_path / "controller.log").read_text()


@pytest.mark.asyncio
async def test_log_sources_reject_unknown_and_report_dynamic_sources() -> None:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/logs/sources")
        assert response.status_code == 200
        ids = [item["id"] for item in response.json()["sources"]]
        assert ids[:2] == ["controller", "daemon"]
        assert "reachy_chat" in ids
        assert (await client.get("/api/logs/stream?source=not-a-source")).status_code == 404
    await app.state.log_broker.aclose()


@pytest.mark.asyncio
async def test_burst_stages_before_scheduling_loop_callbacks() -> None:
    class StalledLoop:
        def __init__(self) -> None:
            self.callbacks = 0

        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, *_args: object) -> None:
            self.callbacks += 1

    broker = LogBroker(queue_size=8)
    broker.bind_source("burst")
    subscription = await broker.subscribe("burst")
    await subscription.__anext__()
    stalled_loop = StalledLoop()
    subscription._loop = stalled_loop

    for index in range(20_000):
        broker.publish("burst", str(index))

    assert stalled_loop.callbacks == 1
    assert len(broker._pending[subscription]) <= broker.queue_size
    await subscription.aclose()
    await broker.aclose()


@pytest.mark.asyncio
async def test_reset_replaces_pending_lines_and_survives_overflow() -> None:
    broker = LogBroker(queue_size=2)
    broker.bind_source("app")
    subscription = await broker.subscribe("app")
    await subscription.__anext__()

    broker.publish("app", "before-reset")
    broker._drain(subscription)
    broker.reset("app", status="live")
    for index in range(20):
        broker.publish("app", f"after-reset-{index}")
    broker._drain(subscription)

    events = list(subscription._queue._queue)
    assert events[0] == {"kind": "reset", "status": "live", "lines": []}
    assert all(event.get("line") != "before-reset" for event in events if isinstance(event, dict))
    await subscription.aclose()
    await broker.aclose()


@pytest.mark.asyncio
async def test_sse_flushes_pre_reset_batch_before_reset() -> None:
    broker = LogBroker()
    broker.bind_source("app")
    subscription = await broker.subscribe("app")
    await subscription.__anext__()
    subscription._queue.put_nowait({"kind": "line", "line": "old"})
    subscription._queue.put_nowait({"kind": "reset", "status": "live", "lines": []})

    stream = _stream_events(broker, subscription)
    first = await stream.__anext__()
    second = await stream.__anext__()
    assert first.startswith("event: log-lines")
    assert second.startswith("event: log-reset")
    await stream.aclose()
    await broker.aclose()


def test_tail_file_caps_single_line_suffix(tmp_path: Path) -> None:
    path = tmp_path / "huge.log"
    path.write_bytes(b"x" * (4 * 1024 * 1024))

    lines = LogBroker.tail_file(path)

    assert len(lines) == 1
    assert len(lines[0].encode()) <= LogBroker.TAIL_BYTES


@pytest.mark.asyncio
async def test_controller_handler_is_owned_by_lifespan() -> None:
    app = create_app()
    assert app.state.controller_log_handler is None

    async with app.router.lifespan_context(app):
        handler = app.state.controller_log_handler
        assert isinstance(handler, ControllerLogHandler)
        assert handler._file is not None
        assert handler in logging.getLogger().handlers

    assert app.state.controller_log_handler is None
    assert handler._file is None
    assert handler not in logging.getLogger().handlers


@pytest.mark.asyncio
async def test_process_pump_bounds_unterminated_output(tmp_path: Path) -> None:
    broker = LogBroker()
    broker.bind_source("huge")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * (4 * 1024 * 1024)); sys.stdout.flush()",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    pump = ProcessLogPump(process, "huge", broker, tmp_path / "huge.log")
    await asyncio.to_thread(process.wait)
    await asyncio.to_thread(pump.stop, 1.0, True)
    assert pump.thread is not None and not pump.thread.is_alive()
    assert pump.max_pending_bytes <= ProcessLogPump.MAX_PARTIAL_LINE_BYTES
    assert pump.max_published_line_bytes <= ProcessLogPump.MAX_PARTIAL_LINE_BYTES
    assert all(
        len(line.encode("utf-8")) <= ProcessLogPump.MAX_PARTIAL_LINE_BYTES
        for line in (tmp_path / "huge.log").read_text().splitlines()
    )
    await broker.aclose()


@pytest.mark.asyncio
async def test_live_publish_during_tail_is_not_replayed_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app()
    broker = app.state.log_broker
    path = tmp_path / "reachy_chat.log"
    path.write_text("persisted\n", encoding="utf-8")
    broker.set_file("reachy_chat", path)
    broker.reset("reachy_chat", status="live")
    entered = asyncio.Event()
    release = asyncio.Event()
    original_tail = broker.tail_file
    loop = asyncio.get_running_loop()

    def delayed_tail(path_arg: Path, limit: int, end: int | None = None) -> list[str]:
        loop.call_soon_threadsafe(entered.set)
        # The broker registration has already captured this file cutoff.
        while not release.is_set():
            time.sleep(0.001)
        return original_tail(path_arg, limit, end=end)

    monkeypatch.setattr(broker, "tail_file", delayed_tail)
    task = asyncio.create_task(log_stream(_stream_request(app), "reachy_chat"))  # type: ignore[arg-type]
    await entered.wait()
    with broker._lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("during-tail\n")
        broker.publish("reachy_chat", "during-tail")
    release.set()
    response = await task
    stream = response.body_iterator
    reset = await stream.__anext__()
    assert json.loads(reset.split("data: ", 1)[1])["lines"] == ["persisted"]
    lines = await stream.__anext__()
    assert json.loads(lines.split("data: ", 1)[1]) == {"lines": ["during-tail"]}
    await stream.aclose()
    await broker.aclose()


@pytest.mark.asyncio
async def test_live_subscription_replays_persisted_tail_for_empty_ring(
    tmp_path: Path,
) -> None:
    app = create_app()
    broker = app.state.log_broker
    path = tmp_path / "reachy_chat.log"
    path.write_text("stale-file-line\n", encoding="utf-8")
    broker.set_file("reachy_chat", path)
    broker.reset("reachy_chat", status="live")

    response = await log_stream(_stream_request(app), "reachy_chat")  # type: ignore[arg-type]
    reset = await response.body_iterator.__anext__()
    assert json.loads(reset.split("data: ", 1)[1]) == {
        "status": "live",
        "lines": ["stale-file-line"],
    }
    await response.body_iterator.aclose()
    await broker.aclose()


@pytest.mark.asyncio
async def test_unavailable_adopted_subscription_replays_persisted_tail(
    tmp_path: Path,
) -> None:
    app = create_app()
    broker = app.state.log_broker
    path = tmp_path / "reachy_chat.log"
    path.write_text("adopted-file-line\n", encoding="utf-8")
    broker.set_file("reachy_chat", path)
    broker.reset("reachy_chat", status="unavailable")

    response = await log_stream(_stream_request(app), "reachy_chat")  # type: ignore[arg-type]
    reset = await response.body_iterator.__anext__()
    assert json.loads(reset.split("data: ", 1)[1]) == {
        "status": "unavailable",
        "lines": ["adopted-file-line"],
    }
    await response.body_iterator.aclose()
    await broker.aclose()


@pytest.mark.asyncio
async def test_offline_daemon_replays_persisted_tail_with_captured_status(
    tmp_path: Path,
) -> None:
    app = create_app()
    broker = app.state.log_broker
    path = tmp_path / "daemon.log"
    path.write_text("offline-file-line\n", encoding="utf-8")
    broker.set_file("daemon", path)
    broker.reset("daemon", status="offline")

    response = await log_stream(_stream_request(app), "daemon")  # type: ignore[arg-type]
    reset = await response.body_iterator.__anext__()
    assert json.loads(reset.split("data: ", 1)[1]) == {
        "status": "offline",
        "lines": ["offline-file-line"],
    }
    await response.body_iterator.aclose()
    await broker.aclose()


@pytest.mark.asyncio
async def test_stopped_app_replays_persisted_tail_with_captured_status(
    tmp_path: Path,
) -> None:
    app = create_app()
    broker = app.state.log_broker
    path = tmp_path / "reachy_chat.log"
    path.write_text("stopped-file-line\n", encoding="utf-8")
    broker.set_file("reachy_chat", path)
    broker.reset("reachy_chat", status="stopped")

    response = await log_stream(_stream_request(app), "reachy_chat")  # type: ignore[arg-type]
    reset = await response.body_iterator.__anext__()
    assert json.loads(reset.split("data: ", 1)[1]) == {
        "status": "stopped",
        "lines": ["stopped-file-line"],
    }
    await response.body_iterator.aclose()
    await broker.aclose()


@pytest.mark.asyncio
async def test_publish_after_cutoff_follows_persisted_reset_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app()
    broker = app.state.log_broker
    path = tmp_path / "daemon.log"
    path.write_text("persisted\n", encoding="utf-8")
    broker.set_file("daemon", path)
    broker.reset("daemon", status="offline")
    entered = threading.Event()
    release = threading.Event()
    original_tail = broker.tail_file

    def delayed_tail(path_arg: Path, limit: int, end: int | None = None) -> list[str]:
        entered.set()
        assert release.wait(1.0)
        return original_tail(path_arg, limit, end=end)

    monkeypatch.setattr(broker, "tail_file", delayed_tail)
    task = asyncio.create_task(log_stream(_stream_request(app), "daemon"))  # type: ignore[arg-type]
    assert await asyncio.to_thread(entered.wait, 1.0)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("after-cutoff\n")
    broker.publish("daemon", "after-cutoff")
    release.set()

    response = await task
    stream = response.body_iterator
    reset = await stream.__anext__()
    assert json.loads(reset.split("data: ", 1)[1]) == {
        "status": "offline",
        "lines": ["persisted"],
    }
    status = await stream.__anext__()
    assert json.loads(status.split("data: ", 1)[1]) == {"status": "live"}
    lines = await stream.__anext__()
    assert json.loads(lines.split("data: ", 1)[1]) == {"lines": ["after-cutoff"]}
    await stream.aclose()
    await broker.aclose()


@pytest.mark.asyncio
async def test_stream_cancellation_during_tail_unregisters_provisional_subscription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app()
    broker = app.state.log_broker
    path = tmp_path / "daemon.log"
    path.write_text("persisted\n", encoding="utf-8")
    broker.set_file("daemon", path)
    broker.reset("daemon", status="offline")
    entered = threading.Event()
    release = threading.Event()

    def blocked_tail(_: Path, __: int, ___: int | None = None) -> list[str]:
        entered.set()
        release.wait(2.0)
        return ["persisted"]

    monkeypatch.setattr(broker, "tail_file", blocked_tail)
    task = asyncio.create_task(log_stream(_stream_request(app), "daemon"))  # type: ignore[arg-type]
    assert await asyncio.to_thread(entered.wait, 1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0.05)
    assert broker._subscribers == {}
    assert broker._pending == {}
    assert broker._scheduled == set()
    await broker.aclose()
