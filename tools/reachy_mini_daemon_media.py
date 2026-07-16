"""Lazy shared MJPEG endpoint for the Reachy Mini daemon's camera IPC feed.

This module is intentionally separate from the Jetson compatibility wrapper.  It
only consumes the daemon's already-published local IPC stream; it never opens a
camera device or constructs a second media server.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

MJPEG_CONTENT_TYPE = "multipart/x-mixed-replace; boundary=frame"
_FRAME_CLOSE = object()


class CameraStreamUnavailable(RuntimeError):
    """Raised when the daemon cannot currently provide local camera media."""


@dataclass
class _Subscriber:
    queue: asyncio.Queue[bytes | object]
    loop: asyncio.AbstractEventLoop


class CameraMJPEGStreamer:
    """Share one blocking ``GStreamerCamera`` reader between HTTP subscribers.

    ``camera_factory`` and ``encoder`` are injectable to keep lifecycle and
    multipart behavior testable without GStreamer, OpenCV, or Jetson hardware.
    The default implementations are imported only when the first stream is
    requested, so the daemon remains usable when optional media packages are
    absent.
    """

    def __init__(
        self,
        *,
        camera_factory: Callable[[], Any] | None = None,
        encoder: Callable[[Any], bytes] | None = None,
        media_ready: Callable[[Any], bool] | None = None,
        retry_interval: float = 0.1,
        startup_timeout: float = 3.0,
    ) -> None:
        self._camera_factory = camera_factory or self._default_camera_factory
        self._encoder = encoder or self._default_encoder
        self._media_ready = media_ready or self._default_media_ready
        self._retry_interval = retry_interval
        self._startup_timeout = startup_timeout
        self._lock = threading.RLock()
        self._latest_jpeg: bytes | None = None
        self._frame_sequence = 0
        self._next_subscriber = 0
        self._subscribers: dict[int, _Subscriber] = {}
        self._reader_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._startup_event: threading.Event | None = None
        self._startup_error: BaseException | None = None
        self._daemon: Any | None = None

    @staticmethod
    def _default_camera_factory() -> Any:
        from reachy_mini.media.camera_gstreamer import GStreamerCamera

        return GStreamerCamera(log_level="WARNING")

    @staticmethod
    def _default_encoder(frame: Any) -> bytes:
        import cv2

        success, encoded = cv2.imencode(".jpg", frame)
        if not success:
            raise RuntimeError("OpenCV could not encode a camera frame")
        return encoded.tobytes()

    @staticmethod
    def _default_media_ready(daemon: Any) -> bool:
        if daemon is None or getattr(daemon, "_media_server", None) is None:
            return False
        released = getattr(daemon, "media_released", False)
        if callable(released):
            released = released()
        if released:
            return False
        try:
            from reachy_mini.daemon.utils import is_local_camera_available

            return bool(is_local_camera_available())
        except Exception:
            return False

    @property
    def latest_jpeg(self) -> bytes | None:
        with self._lock:
            return self._latest_jpeg

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    @property
    def reader_alive(self) -> bool:
        with self._lock:
            return self._reader_thread is not None and self._reader_thread.is_alive()

    @property
    def frame_sequence(self) -> int:
        with self._lock:
            return self._frame_sequence

    def _start_reader_locked(self, daemon: Any) -> threading.Event:
        self._daemon = daemon
        self._stop_event = threading.Event()
        self._startup_event = threading.Event()
        self._startup_error = None
        startup_event = self._startup_event
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            args=(daemon, startup_event),
            name="reachy-camera-mjpeg-reader",
            daemon=True,
        )
        self._reader_thread.start()
        return startup_event

    def _publish(self, jpeg: bytes) -> None:
        chunk = self._multipart_chunk(jpeg)
        with self._lock:
            self._latest_jpeg = jpeg
            self._frame_sequence += 1
            subscribers = list(self._subscribers.values())
        for subscriber in subscribers:
            def deliver(subscriber: _Subscriber = subscriber, chunk: bytes = chunk) -> None:
                token = getattr(subscriber.queue, "_camera_subscriber_token", None)
                with self._lock:
                    if token is None or self._subscribers.get(token) is not subscriber:
                        return
                try:
                    while subscriber.queue.full():
                        subscriber.queue.get_nowait()
                    subscriber.queue.put_nowait(chunk)
                except (asyncio.QueueEmpty, asyncio.QueueFull, RuntimeError):
                    # A disconnected event loop may race the reader thread.
                    pass

            try:
                subscriber.loop.call_soon_threadsafe(deliver)
            except RuntimeError:
                pass

    @staticmethod
    def _close_queue(subscriber: _Subscriber) -> None:
        with contextlib.suppress(asyncio.QueueEmpty):
            while True:
                subscriber.queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            subscriber.queue.put_nowait(_FRAME_CLOSE)

    def _end_subscribers(self) -> None:
        """Signal and remove all subscribers after media readiness is lost."""
        with self._lock:
            subscribers = list(self._subscribers.values())
            self._subscribers.clear()
            self._latest_jpeg = None
            self._stop_event.set()
        for subscriber in subscribers:
            try:
                subscriber.loop.call_soon_threadsafe(self._close_queue, subscriber)
            except RuntimeError:
                pass

    @staticmethod
    def _multipart_chunk(jpeg: bytes) -> bytes:
        return (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
            + jpeg
            + b"\r\n"
        )

    def _reader_loop(self, daemon: Any, startup_event: threading.Event) -> None:
        camera: Any | None = None
        startup_reported = False
        media_lost = False
        try:
            while not self._stop_event.is_set():
                with self._lock:
                    has_subscribers = bool(self._subscribers)
                if not has_subscribers:
                    break
                try:
                    media_ready = bool(self._media_ready(daemon))
                except Exception as exc:
                    logger.warning("Camera media readiness check failed: %s", exc)
                    media_ready = False
                if not media_ready:
                    if camera is not None:
                        with contextlib.suppress(Exception):
                            camera.close()
                        camera = None
                    if not media_lost:
                        media_lost = True
                        self._end_subscribers()
                    self._stop_event.wait(self._retry_interval)
                    continue

                if camera is None:
                    try:
                        camera = self._camera_factory()
                        # ``open`` is deliberately called on this dedicated thread.
                        camera.open()
                        if not startup_reported:
                            startup_reported = True
                            startup_event.set()
                    except BaseException as exc:
                        if not startup_reported:
                            self._startup_error = exc
                            startup_reported = True
                            startup_event.set()
                        logger.warning("Camera IPC reader could not open: %s", exc)
                        if camera is not None:
                            with contextlib.suppress(Exception):
                                camera.close()
                            camera = None
                        self._stop_event.wait(self._retry_interval)
                        continue

                try:
                    frame = camera.read()
                    if frame is None:
                        self._stop_event.wait(0.02)
                        continue
                    jpeg = self._encoder(frame)
                    if jpeg:
                        self._publish(bytes(jpeg))
                except BaseException as exc:
                    logger.warning("Camera IPC reader failed: %s", exc)
                    with contextlib.suppress(Exception):
                        camera.close()
                    camera = None
                    self._stop_event.wait(self._retry_interval)
        finally:
            if not startup_reported:
                self._startup_error = CameraStreamUnavailable("camera reader stopped before opening")
                startup_event.set()
            if camera is not None:
                with contextlib.suppress(Exception):
                    camera.close()
            with self._lock:
                if self._reader_thread is threading.current_thread():
                    self._reader_thread = None
                self._startup_event = None
                self._daemon = None

    async def _join_reader_bounded(self, reader: threading.Thread | None) -> None:
        if reader is None or reader is threading.current_thread():
            return
        try:
            await asyncio.to_thread(reader.join, self._startup_timeout)
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()
            try:
                await asyncio.to_thread(reader.join, self._startup_timeout)
            finally:
                raise

    async def subscribe(self, daemon: Any) -> _Subscriber:
        """Register a subscriber and start the reader if needed."""
        try:
            media_ready = bool(self._media_ready(daemon))
        except Exception as exc:
            raise CameraStreamUnavailable("camera media readiness check failed") from exc
        if not media_ready:
            raise CameraStreamUnavailable("camera media is not ready")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | object] = asyncio.Queue(maxsize=1)
        startup_event: threading.Event | None = None
        stopping_reader: threading.Thread | None = None
        subscriber: _Subscriber
        keep_subscriber = False
        with self._lock:
            self._next_subscriber += 1
            subscriber = _Subscriber(queue=queue, loop=loop)
            token = self._next_subscriber
            self._subscribers[token] = subscriber
            subscriber.queue._camera_subscriber_token = token  # type: ignore[attr-defined]
            reader = self._reader_thread
            if reader is None or not reader.is_alive():
                startup_event = self._start_reader_locked(daemon)
            elif self._stop_event.is_set():
                # A new client can arrive while the last unsubscribe is still
                # joining the old reader.  Wait for that reader to finish
                # before creating a fresh one; otherwise it would exit with
                # the shared stop event already set and the client would 503.
                stopping_reader = reader

        try:
            if stopping_reader is not None:
                await asyncio.to_thread(stopping_reader.join, self._startup_timeout)
                if stopping_reader.is_alive():
                    raise CameraStreamUnavailable("previous camera reader did not stop")
                with self._lock:
                    if self._subscribers.get(token) is not subscriber:
                        raise CameraStreamUnavailable("camera media became unavailable")
                    reader = self._reader_thread
                    if reader is not None and reader.is_alive():
                        raise CameraStreamUnavailable("previous camera reader did not stop")
                    startup_event = self._start_reader_locked(daemon)

            if startup_event is not None:
                ready = await asyncio.to_thread(startup_event.wait, self._startup_timeout)
                with self._lock:
                    startup_error = self._startup_error
                    registered = self._subscribers.get(token) is subscriber
                if not registered:
                    raise CameraStreamUnavailable("camera media became unavailable")
                if not ready:
                    raise CameraStreamUnavailable("camera reader did not become ready")
                if startup_error is not None:
                    raise CameraStreamUnavailable(
                        f"camera reader unavailable: {startup_error}"
                    ) from startup_error
            keep_subscriber = True
            return subscriber
        finally:
            if not keep_subscriber:
                try:
                    await self.unsubscribe(subscriber)
                except asyncio.CancelledError:
                    # The cancellation that interrupted startup also
                    # interrupts an awaited join.  Clear that delivery long
                    # enough to finish the idempotent cleanup, then let the
                    # original cancellation propagate from this finally.
                    current_task = asyncio.current_task()
                    if current_task is not None:
                        current_task.uncancel()
                    await self.unsubscribe(subscriber)

    async def unsubscribe(self, subscriber: _Subscriber) -> None:
        """Remove a subscriber and stop the reader when it was the last one."""
        with self._lock:
            token = getattr(subscriber.queue, "_camera_subscriber_token", None)
            if token is not None and self._subscribers.get(token) is subscriber:
                self._subscribers.pop(token, None)
            should_stop = not self._subscribers
            reader = self._reader_thread if should_stop else None
            if should_stop:
                self._stop_event.set()
        await self._join_reader_bounded(reader)

    async def close_all(self) -> None:
        """Close every dashboard subscriber and stop the shared reader."""
        with self._lock:
            subscribers = list(self._subscribers.values())
            self._subscribers.clear()
            reader = self._reader_thread
            self._stop_event.set()
        for subscriber in subscribers:
            try:
                subscriber.loop.call_soon_threadsafe(self._close_queue, subscriber)
            except RuntimeError:
                pass
        await self._join_reader_bounded(reader)

    async def frames(self, daemon: Any) -> AsyncGenerator[bytes, None]:
        subscriber = await self.subscribe(daemon)
        try:
            while True:
                item = await subscriber.queue.get()
                if item is _FRAME_CLOSE:
                    return
                assert isinstance(item, bytes)
                yield item
        finally:
            await self.unsubscribe(subscriber)

    async def response(self, request: Request) -> StreamingResponse:
        daemon = getattr(request.app.state, "daemon", None)
        stream = self.frames(daemon)
        try:
            # Wait for one frame before sending headers so missing optional
            # dependencies, a dead IPC reader, and encoder errors map honestly
            # to 503 rather than an apparently-live blank response.
            first_chunk = await asyncio.wait_for(
                stream.__anext__(), timeout=self._startup_timeout * 2
            )
        except asyncio.CancelledError:
            await stream.aclose()
            raise
        except (CameraStreamUnavailable, ImportError, RuntimeError, asyncio.TimeoutError, StopAsyncIteration) as exc:
            with contextlib.suppress(Exception):
                await stream.aclose()
            raise HTTPException(status_code=503, detail=f"Camera unavailable: {exc}") from None

        async def body() -> AsyncGenerator[bytes, None]:
            try:
                yield first_chunk
                async for chunk in stream:
                    yield chunk
            finally:
                await stream.aclose()

        return StreamingResponse(body(), headers={"content-type": MJPEG_CONTENT_TYPE})


def install_camera_stream_route() -> None:
    """Wrap daemon ``create_app`` so every daemon app has the MJPEG route."""
    from reachy_mini.daemon.app import main as daemon_main

    original = daemon_main.create_app
    if getattr(original, "_reachy_camera_stream_patch", False):
        return

    def create_app(*args: Any, **kwargs: Any) -> Any:
        app = original(*args, **kwargs)
        streamer = CameraMJPEGStreamer()
        app.state.camera_mjpeg_streamer = streamer

        @app.get("/api/camera/stream.mjpg", include_in_schema=False)
        async def camera_stream(request: Request) -> StreamingResponse:
            return await streamer.response(request)

        return app

    create_app._reachy_camera_stream_patch = True  # type: ignore[attr-defined]
    daemon_main.create_app = create_app
    logger.info("Installed shared daemon camera MJPEG route")
