from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from reachy_controller.main import create_app

_MODULE_PATH = Path(__file__).parents[3] / "tools" / "reachy_mini_daemon_media.py"
_spec = importlib.util.spec_from_file_location("reachy_mini_daemon_media", _MODULE_PATH)
assert _spec and _spec.loader
_media = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _media
_spec.loader.exec_module(_media)
CameraMJPEGStreamer = _media.CameraMJPEGStreamer
CameraStreamUnavailable = _media.CameraStreamUnavailable


class FakeCamera:
    instances: list["FakeCamera"] = []

    def __init__(self) -> None:
        self.closed = False
        self.frames = [b"frame-a", b"frame-b"]
        self.opened = False
        self.__class__.instances.append(self)

    def open(self) -> None:
        self.opened = True

    def read(self) -> bytes:
        if self.closed:
            return b""
        return self.frames[0]

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def clear_fake_cameras() -> None:
    FakeCamera.instances.clear()


class FakeDaemon:
    def __init__(self) -> None:
        self.ready = True
        self._media_server = object()
        self.media_released = False


@pytest.mark.asyncio
async def test_one_reader_shares_latest_multipart_frame_and_stops_on_last_disconnect() -> None:
    daemon = FakeDaemon()
    streamer = CameraMJPEGStreamer(
        camera_factory=FakeCamera,
        encoder=lambda frame: frame,
        media_ready=lambda d: d.ready,
        startup_timeout=1,
    )

    first = await streamer.subscribe(daemon)
    second = await streamer.subscribe(daemon)
    chunk_a = await asyncio.wait_for(first.queue.get(), 1)
    chunk_b = await asyncio.wait_for(second.queue.get(), 1)

    assert chunk_a == chunk_b
    assert chunk_a.startswith(b"--frame\r\nContent-Type: image/jpeg\r\n")
    assert b"Content-Length: 7\r\n\r\nframe-a\r\n" in chunk_a
    assert streamer.subscriber_count == 2
    assert len(FakeCamera.instances) == 1

    await streamer.unsubscribe(first)
    assert streamer.subscriber_count == 1
    assert not FakeCamera.instances[0].closed
    await streamer.unsubscribe(second)
    await asyncio.sleep(0)
    assert streamer.subscriber_count == 0
    assert FakeCamera.instances[0].closed
    assert not streamer.reader_alive


@pytest.mark.asyncio
async def test_media_release_closes_reader_and_reacquire_rebuilds_it() -> None:
    daemon = FakeDaemon()
    streamer = CameraMJPEGStreamer(
        camera_factory=FakeCamera,
        encoder=lambda frame: frame,
        media_ready=lambda d: d.ready,
        startup_timeout=1,
        retry_interval=0.01,
    )
    subscriber = await streamer.subscribe(daemon)
    await asyncio.wait_for(subscriber.queue.get(), 1)

    daemon.ready = False
    await asyncio.sleep(0.05)
    assert FakeCamera.instances[0].closed
    daemon.ready = True
    for _ in range(100):
        if len(FakeCamera.instances) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(FakeCamera.instances) >= 2
    await asyncio.wait_for(subscriber.queue.get(), 1)
    await streamer.unsubscribe(subscriber)


@pytest.mark.asyncio
async def test_blocking_open_runs_off_the_event_loop() -> None:
    class SlowCamera(FakeCamera):
        def open(self) -> None:
            time.sleep(0.08)
            super().open()

    daemon = FakeDaemon()
    streamer = CameraMJPEGStreamer(
        camera_factory=SlowCamera,
        encoder=lambda frame: frame,
        media_ready=lambda d: d.ready,
        startup_timeout=1,
    )
    subscribe_task = asyncio.create_task(streamer.subscribe(daemon))
    ticks = 0
    while not subscribe_task.done():
        ticks += 1
        await asyncio.sleep(0.005)
    subscriber = await subscribe_task
    assert ticks >= 4
    await streamer.unsubscribe(subscriber)


@pytest.mark.asyncio
async def test_unready_media_is_an_honest_503_before_stream_headers() -> None:
    daemon = FakeDaemon()
    daemon.ready = False
    streamer = CameraMJPEGStreamer(
        camera_factory=FakeCamera,
        encoder=lambda frame: frame,
        media_ready=lambda d: d.ready,
        startup_timeout=0.05,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(daemon=daemon)))

    with pytest.raises(HTTPException) as exc_info:
        await streamer.response(request)
    assert exc_info.value.status_code == 503
    assert streamer.subscriber_count == 0


class FakeUpstreamResponse:
    def __init__(self, status_code: int = 200, chunks: tuple[bytes, ...] = (b"a", b"b")) -> None:
        self.status_code = status_code
        self.headers = httpx.Headers({"content-type": "multipart/x-mixed-replace; boundary=frame"})
        self.chunks = chunks
        self.content = b"".join(chunks)

    async def aiter_raw(self):
        for chunk in self.chunks:
            yield chunk

    async def aread(self) -> bytes:
        return self.content


class FakeProxy:
    def __init__(self, response: FakeUpstreamResponse) -> None:
        self.response = response
        self.entered = 0
        self.closed = 0
        self.get_media_status = AsyncMock(return_value={"available": True})
        self.get_daemon_status = AsyncMock(return_value={"running": True})
        self.get_robot_app_lock_status = AsyncMock(return_value={"state": "free"})

    def stream(self, *args: object, **kwargs: object):
        proxy = self

        class Context:
            async def __aenter__(self):
                proxy.entered += 1
                return proxy.response

            async def __aexit__(self, *exc: object) -> None:
                proxy.closed += 1

        return Context()

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_controller_forwards_multipart_chunks_and_releases_lease() -> None:
    proxy = FakeProxy(FakeUpstreamResponse())
    app = create_app(proxy=proxy)  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/camera/stream")

    assert response.status_code == 200
    assert response.headers["content-type"] == "multipart/x-mixed-replace; boundary=frame"
    assert response.content == b"ab"
    assert proxy.entered == 1
    assert proxy.closed == 1
    assert app.state.camera_leases.active_count == 0


@pytest.mark.asyncio
async def test_controller_maps_upstream_failure_and_camera_holder_to_expected_states() -> None:
    proxy = FakeProxy(FakeUpstreamResponse(status_code=503, chunks=(b"offline",)))
    app = create_app(proxy=proxy)  # type: ignore[arg-type]
    app.state.app_supervisor._apps["reachy_chat"].status = "running"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        held = await client.get("/api/camera/stream")
    assert held.status_code == 409
    assert "reachy_chat" in held.json()["detail"]

    app.state.app_supervisor._apps["reachy_chat"].status = "stopped"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        offline = await client.get("/api/camera/stream")
    assert offline.status_code == 503
    assert proxy.closed == 1


@pytest.mark.asyncio
async def test_release_manager_closes_each_active_lease_once() -> None:
    manager = app_manager = __import__(
        "reachy_controller.routes.camera", fromlist=["DashboardCameraLeaseManager"]
    ).DashboardCameraLeaseManager()
    closed: list[str] = []

    async def close_one() -> None:
        closed.append("closed")

    lease = await manager.acquire(close_one)
    await manager.close_all()
    await lease.release()
    assert closed == ["closed"]
    assert manager.active_count == 0
