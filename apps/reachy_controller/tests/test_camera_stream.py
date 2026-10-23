from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from reachy_controller.daemon_proxy import DaemonProxy
from reachy_controller.main import create_app
from reachy_controller.routes import camera as camera_routes
from reachy_controller.routes.camera import DashboardCameraLeaseManager

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
async def test_new_subscriber_during_last_disconnect_starts_fresh_reader() -> None:
    daemon = FakeDaemon()
    streamer = CameraMJPEGStreamer(
        camera_factory=FakeCamera,
        encoder=lambda frame: frame,
        media_ready=lambda d: d.ready,
        startup_timeout=1,
    )

    first = await streamer.subscribe(daemon)
    await asyncio.wait_for(first.queue.get(), 1)
    unsubscribe_task = asyncio.create_task(streamer.unsubscribe(first))
    await asyncio.sleep(0)

    second = await streamer.subscribe(daemon)
    chunk = await asyncio.wait_for(second.queue.get(), 1)
    await unsubscribe_task

    assert chunk.startswith(b"--frame\r\nContent-Type: image/jpeg\r\n")
    assert len(FakeCamera.instances) == 2
    assert FakeCamera.instances[0].closed
    assert not FakeCamera.instances[1].closed
    await streamer.unsubscribe(second)


@pytest.mark.asyncio
async def test_simultaneous_replacement_subscribers_share_one_healthy_reader() -> None:
    class BlockingCloseCamera(FakeCamera):
        close_started = threading.Event()
        allow_close = threading.Event()

        def close(self) -> None:
            self.__class__.close_started.set()
            self.__class__.allow_close.wait(1)
            super().close()

    daemon = FakeDaemon()
    streamer = CameraMJPEGStreamer(
        camera_factory=BlockingCloseCamera,
        encoder=lambda frame: frame,
        media_ready=lambda d: d.ready,
        startup_timeout=1,
    )
    original = await streamer.subscribe(daemon)
    await asyncio.wait_for(original.queue.get(), 1)
    stopping = asyncio.create_task(streamer.unsubscribe(original))
    await asyncio.to_thread(BlockingCloseCamera.close_started.wait, 1)

    followers = [
        asyncio.create_task(streamer.subscribe(daemon)),
        asyncio.create_task(streamer.subscribe(daemon)),
    ]
    for _ in range(100):
        if streamer.subscriber_count == 2:
            break
        await asyncio.sleep(0)
    assert streamer.subscriber_count == 2
    BlockingCloseCamera.allow_close.set()
    first, second = await asyncio.gather(*followers)
    await stopping

    assert len(BlockingCloseCamera.instances) == 2
    assert BlockingCloseCamera.instances[0].closed
    assert not BlockingCloseCamera.instances[1].closed
    assert streamer._reader_thread is not None
    assert streamer._reader_thread.is_alive()
    assert await asyncio.wait_for(first.queue.get(), 1)
    assert await asyncio.wait_for(second.queue.get(), 1)
    await asyncio.gather(streamer.unsubscribe(first), streamer.unsubscribe(second))


@pytest.mark.asyncio
async def test_media_loss_ends_active_subscriber_and_requires_fresh_reconnect() -> None:
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
    while True:
        sentinel = await asyncio.wait_for(subscriber.queue.get(), 1)
        if sentinel is _media._FRAME_CLOSE:
            break
    assert streamer.subscriber_count == 0

    daemon.ready = True
    fresh = await streamer.subscribe(daemon)
    await asyncio.wait_for(fresh.queue.get(), 1)
    assert fresh is not subscriber
    assert len(FakeCamera.instances) == 2
    await streamer.unsubscribe(fresh)


@pytest.mark.asyncio
async def test_stuck_previous_reader_fails_with_bounded_join_and_cleans_provisional_subscriber(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StuckReader:
        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float | None = None) -> None:
            return None

    daemon = FakeDaemon()
    streamer = CameraMJPEGStreamer(media_ready=lambda d: d.ready, startup_timeout=0.02)
    reader = StuckReader()
    streamer._reader_thread = reader  # type: ignore[assignment]
    streamer._stop_event.set()
    join_timeouts: list[float | None] = []

    async def bounded_to_thread(func: object, *args: object) -> None:
        join_timeouts.append(args[0] if args else None)
        func(*args)  # type: ignore[operator]

    monkeypatch.setattr(asyncio, "to_thread", bounded_to_thread)
    with pytest.raises(CameraStreamUnavailable, match="previous camera reader did not stop"):
        await streamer.subscribe(daemon)

    assert streamer.subscriber_count == 0
    assert join_timeouts == [0.02, 0.02]


@pytest.mark.asyncio
async def test_unsubscribe_cancellation_still_finishes_bounded_reader_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingReader:
        def __init__(self) -> None:
            self.joined = False

        def is_alive(self) -> bool:
            return not self.joined

        def join(self, timeout: float | None = None) -> None:
            self.joined = True

    daemon = FakeDaemon()
    streamer = CameraMJPEGStreamer(
        camera_factory=FakeCamera,
        encoder=lambda frame: frame,
        media_ready=lambda d: d.ready,
        startup_timeout=0.02,
    )
    subscriber = await streamer.subscribe(daemon)
    await asyncio.wait_for(subscriber.queue.get(), 1)
    reader = BlockingReader()
    streamer._reader_thread = reader  # type: ignore[assignment]
    streamer._stop_event.set()
    join_started = asyncio.Event()
    release_join = asyncio.Event()
    calls = 0

    async def cancellable_to_thread(func: object, *args: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            join_started.set()
            await release_join.wait()
        func(*args)  # type: ignore[operator]

    monkeypatch.setattr(asyncio, "to_thread", cancellable_to_thread)
    task = asyncio.create_task(streamer.unsubscribe(subscriber))
    await join_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert streamer.subscriber_count == 0
    assert calls == 2
    assert reader.joined


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


@pytest.mark.asyncio
async def test_subscribe_cancellation_during_reader_join_removes_subscriber(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingReader:
        def __init__(self) -> None:
            self.joined = False
            self.closed = False

        def is_alive(self) -> bool:
            return not self.joined

        def join(self, _timeout: float | None = None) -> None:
            self.joined = True
            self.closed = True

    daemon = FakeDaemon()
    streamer = CameraMJPEGStreamer(media_ready=lambda d: d.ready)
    reader = BlockingReader()
    streamer._reader_thread = reader  # type: ignore[assignment]
    streamer._stop_event.set()
    join_started = asyncio.Event()
    release_join = asyncio.Event()
    calls = 0

    async def blocking_to_thread(func: object, *args: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            join_started.set()
            await release_join.wait()
        else:
            func(*args)  # type: ignore[operator]

    monkeypatch.setattr(asyncio, "to_thread", blocking_to_thread)
    task = asyncio.create_task(streamer.subscribe(daemon))
    await join_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert streamer.subscriber_count == 0
    assert reader.joined
    assert reader.closed
    assert not streamer.reader_alive


@pytest.mark.asyncio
async def test_startup_timeout_removes_subscriber_and_stops_reader() -> None:
    daemon = FakeDaemon()
    checks = 0

    def media_ready(_daemon: FakeDaemon) -> bool:
        nonlocal checks
        checks += 1
        return checks == 1

    streamer = CameraMJPEGStreamer(
        camera_factory=FakeCamera,
        encoder=lambda frame: frame,
        media_ready=media_ready,
        startup_timeout=0.02,
        retry_interval=0.001,
    )

    with pytest.raises(CameraStreamUnavailable, match="camera media became unavailable"):
        await streamer.subscribe(daemon)

    assert streamer.subscriber_count == 0
    assert not streamer.reader_alive
    assert streamer._stop_event.is_set()


@pytest.mark.asyncio
async def test_daemon_stream_keeps_connect_timeout_and_disables_read_timeout() -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.timeout: httpx.Timeout | None = None

        def stream(self, *args: object, **kwargs: object):
            self.timeout = kwargs["timeout"]  # type: ignore[assignment]

            class Context:
                async def __aenter__(self) -> object:
                    return SimpleNamespace()

                async def __aexit__(self, *exc: object) -> None:
                    return None

            return Context()

    proxy = DaemonProxy("http://daemon", timeout=2.0)
    client = RecordingClient()
    proxy._client = client  # type: ignore[assignment]

    async with proxy.stream("GET", "camera/stream.mjpg"):
        pass

    assert client.timeout is not None
    assert client.timeout.connect == 2.0
    assert client.timeout.read is None
    assert client.timeout.write == 2.0
    assert client.timeout.pool == 2.0


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
        self.on_enter = None

    def stream(self, *args: object, **kwargs: object):
        proxy = self

        class Context:
            async def __aenter__(self):
                proxy.entered += 1
                if proxy.on_enter is not None:
                    proxy.on_enter()
                return proxy.response

            async def __aexit__(self, *exc: object) -> None:
                proxy.closed += 1

        return Context()

    async def close(self) -> None:
        return None


class NonTerminatingResponse(FakeUpstreamResponse):
    def __init__(self) -> None:
        super().__init__(chunks=())
        self.release = asyncio.Event()

    async def aiter_raw(self):
        yield b"first"
        await self.release.wait()


class GatedCloseProxy(FakeProxy):
    def __init__(self, response: FakeUpstreamResponse) -> None:
        super().__init__(response)
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()

    def stream(self, *args: object, **kwargs: object):
        proxy = self

        class Context:
            async def __aenter__(self):
                proxy.entered += 1
                if proxy.on_enter is not None:
                    proxy.on_enter()
                return proxy.response

            async def __aexit__(self, *exc: object) -> None:
                proxy.close_started.set()
                await proxy.allow_close.wait()
                proxy.closed += 1

        return Context()


@pytest.mark.asyncio
async def test_controller_startup_deadline_closes_stalled_upstream() -> None:
    class StallingProxy(FakeProxy):
        def stream(self, *args: object, **kwargs: object):
            proxy = self

            class Context:
                async def __aenter__(self):
                    proxy.entered += 1
                    await asyncio.sleep(3600)

                async def __aexit__(self, *exc: object) -> None:
                    proxy.closed += 1

            return Context()

    proxy = StallingProxy(FakeUpstreamResponse())
    app = create_app(proxy=proxy)  # type: ignore[arg-type]
    with patch.object(camera_routes, "CAMERA_STREAM_STARTUP_TIMEOUT_S", 0.01):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/camera/stream")

    assert response.status_code == 503
    assert proxy.entered == 1
    assert proxy.closed == 1
    assert app.state.camera_leases.active_count == 0


@pytest.mark.asyncio
async def test_controller_startup_deadline_closes_stalled_first_chunk() -> None:
    class StallingResponse(FakeUpstreamResponse):
        async def aiter_raw(self):
            await asyncio.sleep(3600)
            yield b"never"

    proxy = FakeProxy(StallingResponse())
    app = create_app(proxy=proxy)  # type: ignore[arg-type]
    with patch.object(camera_routes, "CAMERA_STREAM_STARTUP_TIMEOUT_S", 0.01):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/camera/stream")

    assert response.status_code == 503
    assert proxy.entered == 1
    assert proxy.closed == 1
    assert app.state.camera_leases.active_count == 0


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
async def test_controller_client_disconnect_closes_upstream() -> None:
    proxy = FakeProxy(FakeUpstreamResponse(chunks=(b"first",)))
    app = create_app(proxy=proxy)  # type: ignore[arg-type]
    body_sent = asyncio.Event()
    receive_calls = 0

    async def receive() -> dict[str, object]:
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls == 1:
            return {"type": "http.request", "body": b"", "more_body": False}
        await body_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            body_sent.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/camera/stream",
        "raw_path": b"/api/camera/stream",
        "query_string": b"",
        "headers": [],
        "client": ("test", 1234),
        "server": ("test", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), 1)

    assert proxy.closed == 1
    assert app.state.camera_leases.active_count == 0


@pytest.mark.asyncio
async def test_nonterminating_client_disconnect_waits_for_shielded_upstream_close() -> None:
    proxy = GatedCloseProxy(NonTerminatingResponse())
    app = create_app(proxy=proxy)  # type: ignore[arg-type]
    body_sent = asyncio.Event()
    receive_calls = 0

    async def receive() -> dict[str, object]:
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls == 1:
            return {"type": "http.request", "body": b"", "more_body": False}
        await body_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            body_sent.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/camera/stream",
        "raw_path": b"/api/camera/stream",
        "query_string": b"",
        "headers": [],
        "client": ("test", 1234),
        "server": ("test", 80),
    }
    app_task = asyncio.create_task(app(scope, receive, send))
    await body_sent.wait()
    await proxy.close_started.wait()
    assert not app_task.done()
    assert app.state.camera_leases.active_count == 1

    proxy.allow_close.set()
    await asyncio.wait_for(app_task, 1)
    assert proxy.closed == 1
    assert app.state.camera_leases.active_count == 0


@pytest.mark.asyncio
async def test_camera_takeover_close_all_awaits_inflight_route_cleanup() -> None:
    proxy = GatedCloseProxy(NonTerminatingResponse())
    app = create_app(proxy=proxy)  # type: ignore[arg-type]
    request = SimpleNamespace(app=app)
    response = await camera_routes.camera_stream(request)  # type: ignore[arg-type]
    first_chunk = await response.body_iterator.__anext__()
    assert first_chunk == b"first"

    route_close = asyncio.create_task(response.body_iterator.aclose())
    await proxy.close_started.wait()
    takeover_close = asyncio.create_task(app.state.camera_leases.close_all())
    await asyncio.sleep(0)
    assert not takeover_close.done()
    assert app.state.camera_leases.active_count == 1
    assert not route_close.done()

    proxy.allow_close.set()
    await asyncio.wait_for(asyncio.gather(route_close, takeover_close), 1)
    assert proxy.closed == 1
    assert app.state.camera_leases.active_count == 0


@pytest.mark.asyncio
async def test_running_vision_app_does_not_block_dashboard_stream() -> None:
    proxy = FakeProxy(FakeUpstreamResponse())
    app = create_app(proxy=proxy)  # type: ignore[arg-type]
    app.state.daemon_apps._current = {"info": {"name": "vision_app"}, "state": "running"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/camera/stream")

    assert response.status_code == 200
    assert response.content == b"ab"
    assert proxy.entered == 1
    assert proxy.closed == 1
    assert app.state.camera_leases.active_count == 0


@pytest.mark.asyncio
async def test_upstream_failure_is_forwarded_while_vision_app_runs() -> None:
    proxy = FakeProxy(FakeUpstreamResponse(status_code=503, chunks=(b"offline",)))
    app = create_app(proxy=proxy)  # type: ignore[arg-type]
    app.state.daemon_apps._current = {"info": {"name": "vision_app"}, "state": "running"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/camera/stream")

    assert response.status_code == 503
    assert proxy.closed == 1


@pytest.mark.asyncio
async def test_release_manager_closes_each_active_lease_once() -> None:
    manager = DashboardCameraLeaseManager()
    closed: list[str] = []

    async def close_one() -> None:
        closed.append("closed")

    lease = await manager.acquire(close_one)
    await manager.close_all()
    await lease.release()
    assert closed == ["closed"]
    assert manager.active_count == 0
