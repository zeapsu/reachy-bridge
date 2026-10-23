"""Lazy shared MJPEG endpoint for the Reachy Mini daemon's camera IPC feed.

This module is intentionally separate from the Jetson compatibility wrapper.  It
only consumes the daemon's already-published local IPC stream; it never opens a
camera device or constructs a second media server.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import platform
import threading
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

MJPEG_CONTENT_TYPE = "multipart/x-mixed-replace; boundary=frame"
_FRAME_CLOSE = object()
_SPECTRUM_BINS_PER_BAND = 6
_SPECTRUM_BAND_RANGES_HZ = ((80.0, 300.0), (300.0, 2_000.0), (2_000.0, 7_500.0))
_SPECTRUM_BIN_RANGES_HZ = tuple(
    (
        low
        if index == 0
        else low + ((high - low) / (_SPECTRUM_BINS_PER_BAND - 1)) * (index - 0.5),
        high
        if index == _SPECTRUM_BINS_PER_BAND - 1
        else low + ((high - low) / (_SPECTRUM_BINS_PER_BAND - 1)) * (index + 0.5),
    )
    for low, high in _SPECTRUM_BAND_RANGES_HZ
    for index in range(_SPECTRUM_BINS_PER_BAND)
)


class AudioSpectrumTap:
    """Compute a bounded, latest-only spectrum from the daemon mic pipeline.
    GStreamer invokes :meth:`process_samples` from its streaming thread.  The
    method does not touch asyncio and keeps a reusable 2048-sample rolling
    buffer plus reusable FFT work arrays.  The returned spectrum contains six
    measured FFT regions in each of the low, mid, and high ranges; no
    interpolation or synthetic profile is applied.
    """

    ANALYSIS_WINDOW_SIZE = 2_048
    DB_FLOOR = -60.0
    DB_CEILING = -6.0
    SOURCE = "daemon mic tap"
    BAND_RANGES_HZ = _SPECTRUM_BAND_RANGES_HZ
    BINS_PER_BAND = _SPECTRUM_BINS_PER_BAND
    SPECTRUM_BIN_RANGES_HZ = _SPECTRUM_BIN_RANGES_HZ
    SPECTRUM_BIN_COUNT = len(SPECTRUM_BIN_RANGES_HZ)

    def __init__(
        self,
        *,
        compute_interval: float = 0.05,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if compute_interval < 0:
            raise ValueError("compute_interval must be non-negative")
        self.compute_interval = compute_interval
        self._clock = clock
        self._lock = threading.Lock()
        self._latest: dict[str, Any] | None = None
        self._latest_monotonic: float | None = None
        self._last_compute_monotonic: float | None = None
        self._cache_key: tuple[int, int] | None = None
        self._window: np.ndarray | None = None
        self._windowed: np.ndarray | None = None
        self._magnitude: np.ndarray | None = None
        self._power: np.ndarray | None = None
        self._window_sum = 1.0
        self._magnitude_scale = 2.0
        self._spectrum_bin_slices: tuple[tuple[int, int], ...] | None = None
        self._rolling_samples = np.empty(self.ANALYSIS_WINDOW_SIZE, dtype=np.float32)
        self._contiguous_samples = np.empty_like(self._rolling_samples)
        self._rolling_write_index = 0
        self._rolling_count = 0
        self._rolling_sample_rate_hz: int | None = None
        self._rms_squared = np.empty_like(self._contiguous_samples, dtype=np.float64)
        self._finite_mask: np.ndarray | None = None
        self.compute_count = 0
        self.fft_count = 0
        self.frequency_cache_builds = 0

    @property
    def latest(self) -> dict[str, Any] | None:
        """Return a defensive copy of the latest computed sample."""
        with self._lock:
            return None if self._latest is None else dict(self._latest)

    @property
    def latest_age(self) -> float | None:
        with self._lock:
            if self._latest_monotonic is None:
                return None
            return max(0.0, self._clock() - self._latest_monotonic)

    def latest_if_fresh(self, *, max_age: float = 1.0) -> dict[str, Any] | None:
        """Return the latest sample only while it is younger than ``max_age``."""
        with self._lock:
            if self._latest is None or self._latest_monotonic is None:
                return None
            if self._clock() - self._latest_monotonic > max_age:
                return None
            return dict(self._latest)

    @classmethod
    def _db_to_level(cls, dbfs: float) -> float:
        if not math.isfinite(dbfs):
            return 0.0
        return max(0.0, min(1.0, (dbfs - cls.DB_FLOOR) / (cls.DB_CEILING - cls.DB_FLOOR)))

    @classmethod
    def _clamped_dbfs(cls, amplitude: float) -> float:
        if not math.isfinite(amplitude) or amplitude <= 1e-12:
            return cls.DB_FLOOR
        return max(cls.DB_FLOOR, min(0.0, 20.0 * math.log10(amplitude)))

    def _ensure_frequency_cache(self, sample_count: int, sample_rate_hz: int) -> None:
        key = (sample_count, sample_rate_hz)
        if key == self._cache_key:
            return
        window = np.hanning(sample_count).astype(np.float32)
        frequencies = np.fft.rfftfreq(sample_count, d=1.0 / sample_rate_hz)
        bin_slices = tuple(
            (
                int(np.searchsorted(frequencies, low, side="left")),
                int(np.searchsorted(frequencies, high, side="left")),
            )
            for low, high in self.SPECTRUM_BIN_RANGES_HZ
        )
        self._cache_key = key
        self._window = window
        self._windowed = np.empty_like(window)
        self._magnitude = np.empty(frequencies.shape, dtype=np.float32)
        self._power = np.empty_like(self._magnitude)
        self._window_sum = max(float(np.sum(window, dtype=np.float64)), 1e-12)
        # For positive, non-DC/non-Nyquist FFT bins, 2 / sum(window)
        # recovers a sinusoid's peak amplitude.  Converting summed squared
        # amplitudes by sqrt(1/2) therefore yields its RMS amplitude.
        self._magnitude_scale = 2.0 / self._window_sum
        self._spectrum_bin_slices = bin_slices
        self.frequency_cache_builds += 1

    def _ingest_samples(self, array: np.ndarray, sample_rate_hz: int) -> bool:
        """Append one mapped audio chunk and expose a contiguous full window."""
        if self._rolling_sample_rate_hz != sample_rate_hz:
            self._rolling_sample_rate_hz = sample_rate_hz
            self._rolling_write_index = 0
            self._rolling_count = 0

        window_size = self.ANALYSIS_WINDOW_SIZE
        sample_count = len(array)
        if sample_count >= window_size:
            np.copyto(self._rolling_samples, array[-window_size:])
            self._rolling_write_index = 0
            self._rolling_count = window_size
        else:
            write_index = self._rolling_write_index
            first_count = min(sample_count, window_size - write_index)
            np.copyto(
                self._rolling_samples[write_index : write_index + first_count],
                array[:first_count],
            )
            remaining = sample_count - first_count
            if remaining:
                np.copyto(self._rolling_samples[:remaining], array[first_count:])
            self._rolling_write_index = (write_index + sample_count) % window_size
            self._rolling_count = min(window_size, self._rolling_count + sample_count)

        return self._rolling_count >= window_size

    def _copy_rolling_window(self) -> None:
        """Copy the current ring into chronological contiguous order."""
        write_index = self._rolling_write_index
        if write_index:
            first_count = self.ANALYSIS_WINDOW_SIZE - write_index
            np.copyto(
                self._contiguous_samples[:first_count],
                self._rolling_samples[write_index:],
            )
            np.copyto(
                self._contiguous_samples[first_count:],
                self._rolling_samples[:write_index],
            )
        else:
            np.copyto(self._contiguous_samples, self._rolling_samples)

    def process_samples(
        self,
        samples: Any,
        *,
        sample_rate_hz: int = 16_000,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Ingest one mono float32 chunk and rate-cap rolling-window FFT work.

        ``samples`` should be a contiguous float32 view from a mapped Gst
        buffer.  Other array-like inputs are accepted for unit tests and are
        converted only when necessary.  Chunks are always ingested before the
        20 Hz compute gate so short callbacks cannot starve the analysis
        window.
        """
        array = np.asarray(samples)
        if array.size == 0:
            return None
        if array.ndim != 1:
            array = array.reshape(-1)
        if array.dtype != np.float32:
            array = array.astype(np.float32, copy=False)
        if self._finite_mask is None or len(self._finite_mask) != len(array):
            self._finite_mask = np.empty(array.shape, dtype=np.bool_)
        if not np.isfinite(array, out=self._finite_mask).all():
            array = np.nan_to_num(array, copy=True, nan=0.0, posinf=0.0, neginf=0.0)
        sample_rate = max(1, int(sample_rate_hz))
        if not self._ingest_samples(array, sample_rate):
            return None
        compute_time = self._clock() if now is None else now

        with self._lock:
            previous = self._last_compute_monotonic
            if previous is not None and compute_time - previous < self.compute_interval:
                return None
            self._last_compute_monotonic = compute_time
        self._copy_rolling_window()

        self._ensure_frequency_cache(self.ANALYSIS_WINDOW_SIZE, sample_rate)
        assert self._window is not None
        assert self._windowed is not None
        assert self._magnitude is not None
        assert self._power is not None
        np.square(self._contiguous_samples, out=self._rms_squared)
        rms = math.sqrt(float(np.mean(self._rms_squared)))

        rms_db = self._clamped_dbfs(rms)
        np.multiply(self._contiguous_samples, self._window, out=self._windowed)
        fft_values = np.fft.rfft(self._windowed)
        np.abs(fft_values, out=self._magnitude)
        self._magnitude *= self._magnitude_scale
        np.square(self._magnitude, out=self._power)

        spectrum_levels = [0.0] * self.SPECTRUM_BIN_COUNT
        low_power = 0.0
        mid_power = 0.0
        high_power = 0.0
        for index, (start, end) in enumerate(self._spectrum_bin_slices):
            power = (
                float(np.sum(self._power[start:end], dtype=np.float64))
                if end > start
                else 0.0
            )
            spectrum_levels[index] = self._db_to_level(
                self._clamped_dbfs(math.sqrt(power * 0.5))
            )
            if index < self.BINS_PER_BAND:
                low_power += power
            elif index < self.BINS_PER_BAND * 2:
                mid_power += power
            else:
                high_power += power

        snapshot = {
            "rms": self._db_to_level(rms_db),
            "rms_db": rms_db,
            "low": self._db_to_level(self._clamped_dbfs(math.sqrt(low_power * 0.5))),
            "mid": self._db_to_level(self._clamped_dbfs(math.sqrt(mid_power * 0.5))),
            "high": self._db_to_level(self._clamped_dbfs(math.sqrt(high_power * 0.5))),
            "spectrum": spectrum_levels,
            "sample_rate_hz": sample_rate,
            "source": self.SOURCE,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        with self._lock:
            self._latest = snapshot
            self._latest_monotonic = compute_time
            self.compute_count += 1
            self.fft_count += 1
        return dict(snapshot)


def apply_jetson_audio_spectrum_patch() -> None:
    """Tee the daemon-owned mic capture into one bounded analysis appsink."""
    if not _is_jetson():
        return
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as exc:
        logger.warning("GStreamer unavailable; skipping audio spectrum tap: %s", exc)
        return
    try:
        from reachy_mini.media.media_server import GstMediaServer
    except ImportError as exc:
        logger.warning("Reachy Mini media server unavailable: %s", exc)
        return

    if getattr(GstMediaServer, "_reachy_audio_spectrum_patched", False):
        return
    original_configure_audio = GstMediaServer._configure_audio

    def _configure_audio(self: Any, pipeline: Any, webrtcsink: Any) -> None:
        self._reachy_audio_spectrum_tap = None
        original_configure_audio(self, pipeline, webrtcsink)
        queue_audiosrc = pipeline.get_by_name("queue_audiosrc")
        if queue_audiosrc is None:
            return

        element_specs = (
            ("tee", "audio_capture_tee"),
            ("queue", "queue_audio_webrtc"),
            ("queue", "queue_audio_analysis"),
            ("audioconvert", "audio_spectrum_convert"),
            ("audioresample", "audio_spectrum_resample"),
            ("capsfilter", "audio_spectrum_caps"),
            ("appsink", "audio_spectrum_sink"),
        )
        elements: list[Any] = []
        for factory_name, element_name in element_specs:
            element = Gst.ElementFactory.make(factory_name, element_name)
            if element is None:
                logger.warning(
                    "Audio spectrum unavailable: missing GStreamer element %s",
                    factory_name,
                )
                return
            elements.append(element)
        tee, queue_webrtc, queue_analysis, convert, resample, capsfilter, appsink = elements
        added: list[Any] = []
        links = (
            (queue_audiosrc, tee),
            (tee, queue_webrtc),
            (queue_webrtc, webrtcsink),
            (tee, queue_analysis),
            (queue_analysis, convert),
            (convert, resample),
            (resample, capsfilter),
            (capsfilter, appsink),
        )

        def _rollback() -> None:
            for source, target in links:
                with contextlib.suppress(Exception):
                    source.unlink(target)
            remove = getattr(pipeline, "remove", None)
            if remove is not None:
                for element in reversed(added):
                    with contextlib.suppress(Exception):
                        remove(element)
            try:
                if not queue_audiosrc.link(webrtcsink):
                    logger.error("Could not restore direct queue_audiosrc to WebRTC audio link")
            except Exception:
                logger.exception("Could not restore direct queue_audiosrc to WebRTC audio link")

        try:
            queue_audiosrc.unlink(webrtcsink)
            for element in elements:
                added.append(element)
                if not pipeline.add(element):
                    raise RuntimeError(f"Could not add audio spectrum element {element!r}")
            for source, target in links:
                if not source.link(target):
                    raise RuntimeError(
                        f"Could not link audio spectrum elements {source!r} -> {target!r}"
                    )
            capsfilter.set_property(
                "caps",
                Gst.Caps.from_string("audio/x-raw,format=F32LE,channels=1,rate=16000"),
            )
            queue_analysis.set_property("leaky", 2)
            queue_analysis.set_property("max-size-buffers", 2)
            queue_analysis.set_property("max-size-bytes", 0)
            queue_analysis.set_property("max-size-time", 0)
            appsink.set_property("emit-signals", True)
            appsink.set_property("sync", False)
            appsink.set_property("max-buffers", 2)
            appsink.set_property("drop", True)
        except Exception:
            logger.exception("Could not install audio spectrum branch; keeping WebRTC audio")
            _rollback()
            return

        tap = AudioSpectrumTap()

        def _on_new_sample(sink: Any) -> Any:
            try:
                sample = sink.emit("pull-sample")
                if sample is None:
                    return getattr(Gst.FlowReturn, "OK", 0)
                buffer = sample.get_buffer()
                mapped, map_info = buffer.map(Gst.MapFlags.READ)
                if not mapped:
                    return getattr(Gst.FlowReturn, "OK", 0)
                try:
                    samples = np.frombuffer(map_info.data, dtype=np.float32)
                    tap.process_samples(samples, sample_rate_hz=16_000)
                finally:
                    buffer.unmap(map_info)
            except Exception:
                logger.debug("Audio spectrum sample processing failed", exc_info=True)
            return getattr(Gst.FlowReturn, "OK", 0)

        appsink.connect("new-sample", _on_new_sample)
        self._reachy_audio_spectrum_tap = tap

    GstMediaServer._configure_audio = _configure_audio
    GstMediaServer._reachy_audio_spectrum_patched = True
    logger.info("Installed shared daemon audio spectrum tap")


def _is_jetson() -> bool:
    """Return whether this process is running on an NVIDIA Jetson board."""
    if platform.system() != "Linux":
        return False
    try:
        with open("/proc/device-tree/model", "r") as model_file:
            model = model_file.read()
    except OSError:
        return False
    return "Tegra" in model or "NVIDIA" in model


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
                await self._join_reader_bounded(stopping_reader)
                if stopping_reader.is_alive():
                    raise CameraStreamUnavailable("previous camera reader did not stop")
                with self._lock:
                    if self._subscribers.get(token) is not subscriber:
                        raise CameraStreamUnavailable("camera media became unavailable")
                    reader = self._reader_thread
                    if reader is not None and reader.is_alive():
                        # Another subscriber may have won the replacement race.
                        # Followers share that reader's startup event rather
                        # than treating the healthy replacement as stuck.
                        if reader is stopping_reader or self._stop_event.is_set():
                            raise CameraStreamUnavailable("previous camera reader did not stop")
                        startup_event = self._startup_event
                        if startup_event is None:
                            raise CameraStreamUnavailable("camera reader stopped during replacement")
                    else:
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
 
def install_audio_levels_route() -> None:
    """Wrap daemon ``create_app`` with the fresh mic-level endpoint."""
    from reachy_mini.daemon.app import main as daemon_main

    original = daemon_main.create_app
    if getattr(original, "_reachy_audio_levels_route_patch", False):
        return

    def create_app(*args: Any, **kwargs: Any) -> Any:
        app = original(*args, **kwargs)

        @app.get("/api/audio/levels", include_in_schema=False)
        async def audio_levels(request: Request) -> dict[str, Any]:
            daemon = getattr(request.app.state, "daemon", None)
            if daemon is None:
                raise HTTPException(status_code=503, detail="Audio media unavailable")
            released = getattr(daemon, "media_released", False)
            if callable(released):
                released = released()
            if released:
                raise HTTPException(status_code=503, detail="Audio media released")
            media_server = getattr(daemon, "_media_server", None)
            tap = getattr(media_server, "_reachy_audio_spectrum_tap", None)
            if tap is None:
                raise HTTPException(status_code=503, detail="Audio spectrum unavailable")
            levels = tap.latest_if_fresh(max_age=1.0)
            if levels is None:
                raise HTTPException(status_code=503, detail="Audio spectrum is stale")
            return levels

        return app

    create_app._reachy_audio_levels_route_patch = True  # type: ignore[attr-defined]
    daemon_main.create_app = create_app
    logger.info("Installed daemon audio levels route")
