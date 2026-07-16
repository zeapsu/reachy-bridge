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
    app.state.controller_log_handler.close()
