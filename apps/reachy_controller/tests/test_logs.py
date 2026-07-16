"""Focused bounded log broker, pump, handler, and source route tests."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

import httpx
import pytest

from reachy_controller.log_stream import ControllerLogHandler, LogBroker, ProcessLogPump
from reachy_controller.main import create_app
from reachy_controller.routes.logs import _stream_events


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
    await asyncio.to_thread(pump.stop)
    assert (tmp_path / "burst.log").read_text().splitlines() == ["stdout", "stderr"]
    assert broker.ring("burst") == ["stdout", "stderr"]
    assert pump.thread is not None and not pump.thread.is_alive()
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
