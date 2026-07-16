"""Bounded, thread-safe log fan-out and subprocess output pumps."""

from __future__ import annotations

import asyncio
import codecs
import logging
import os
import select
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, TextIO


_CLOSED = object()


class LogSubscription:
    """One bounded asynchronous subscription to a source's log events."""

    def __init__(self, broker: LogBroker, source: str, queue: asyncio.Queue[Any]) -> None:
        self._broker = broker
        self.source = source
        self._queue = queue
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._initial_lines: list[str] = []
        self._initial_status = "empty"
        self._file_end: int | None = None

    @property
    def initial_status(self) -> str:
        return self._initial_status

    @property
    def file_end(self) -> int | None:
        return self._file_end

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._closed:
            raise StopAsyncIteration
        value = await self._queue.get()
        if value is _CLOSED:
            self._closed = True
            raise StopAsyncIteration
        return value

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._broker.unsubscribe(self)


class LogBroker:
    """Thread-safe bounded per-source rings and per-client async fan-out."""

    TAIL_BYTES = 128 * 1024

    def __init__(self, *, ring_size: int = 500, queue_size: int = 256) -> None:
        if ring_size < 1 or queue_size < 1:
            raise ValueError("ring_size and queue_size must be positive")
        self.ring_size = ring_size
        self.queue_size = queue_size
        self._rings: dict[str, deque[str]] = {}
        self._statuses: dict[str, str] = {}
        self._files: dict[str, Path] = {}
        self._subscribers: dict[LogSubscription, asyncio.Queue[Any]] = {}
        # Pumps publish from arbitrary reader threads.  Stage a bounded amount
        # per subscriber before touching the event loop, and schedule one drain
        # callback per subscriber at a time.
        self._pending: dict[LogSubscription, deque[dict[str, Any]]] = {}
        self._scheduled: set[LogSubscription] = set()
        self._lock = threading.RLock()
        self._closed = False

    def bind_source(self, source: str, *, status: str = "empty", file_path: Path | None = None) -> None:
        with self._lock:
            self._rings.setdefault(source, deque(maxlen=self.ring_size))
            self._statuses.setdefault(source, status)
            if file_path is not None:
                self._files[source] = file_path

    def set_file(self, source: str, file_path: Path) -> None:
        with self._lock:
            self._files[source] = file_path

    def source_file(self, source: str) -> Path | None:
        with self._lock:
            return self._files.get(source)

    def source_status(self, source: str) -> str:
        with self._lock:
            return self._statuses.get(source, "empty")

    def sources(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._rings)

    def ring(self, source: str) -> list[str]:
        with self._lock:
            return list(self._rings.get(source, ()))

    def replay(self, source: str, limit: int = 200) -> list[str]:
        with self._lock:
            return list(self._rings.get(source, ()))[-limit:]

    def set_status(self, source: str, status: str) -> None:
        with self._lock:
            self._rings.setdefault(source, deque(maxlen=self.ring_size))
            previous = self._statuses.get(source, "empty")
            self._statuses[source] = status
        if previous != status:
            self._schedule(source, {"kind": "status", "status": status})

    def reset(self, source: str, *, status: str | None = None) -> None:
        with self._lock:
            self._rings.setdefault(source, deque(maxlen=self.ring_size)).clear()
            current = self._statuses.get(source, "empty")
            if status is not None:
                self._statuses[source] = status
            current = self._statuses.get(source, current)
        # _stage_event replaces all pending data/control events with this
        # protected reset.  The drain also clears an already-full client queue
        # before inserting it, so a later burst cannot evict the reset.
        self._schedule(source, {"kind": "reset", "status": current, "lines": []})

    def publish(self, source: str, line: str) -> None:
        """Append a line immediately, then fan it out on each subscriber loop."""
        text = line.rstrip("\r\n")
        with self._lock:
            self._rings.setdefault(source, deque(maxlen=self.ring_size)).append(text)
            previous = self._statuses.get(source, "empty")
            if previous in ("empty", "stopped", "offline", "unavailable"):
                self._statuses[source] = "live"
            status_changed = previous != "live"
        if status_changed:
            self._schedule(source, {"kind": "status", "status": "live"})
        self._schedule(source, {"kind": "line", "line": text})

    @staticmethod
    def _file_end_locked(path: Path | None) -> int | None:
        if path is None:
            return None
        try:
            return path.stat().st_size
        except OSError:
            return None

    async def subscribe(
        self,
        source: str,
        *,
        replay: list[str] | None = None,
        defer_initial: bool = False,
    ) -> LogSubscription:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._closed:
                raise RuntimeError("log broker is closed")
            self._rings.setdefault(source, deque(maxlen=self.ring_size))
            status = self._statuses.get(source, "empty")
            source_path = self._files.get(source)
            file_end = self._file_end_locked(source_path)
            lines = self.replay(source) if replay is None else list(replay[-200:])
            queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self.queue_size)
            subscription = LogSubscription(self, source, queue)
            subscription._loop = loop
            subscription._initial_lines = lines
            subscription._initial_status = status
            subscription._file_end = file_end
            self._subscribers[subscription] = queue
            self._pending[subscription] = deque()
            if not defer_initial:
                queue.put_nowait({"kind": "reset", "status": status, "lines": lines})
            return subscription

    async def initialize(
        self,
        subscription: LogSubscription,
        *,
        lines: list[str],
        status: str,
    ) -> None:
        """Emit one initial reset after an optional persisted tail read.

        Registration occurs before the tail read.  Anything published while that
        read is in progress is moved behind this reset without dropping it.
        """
        with self._lock:
            queue = self._subscribers.get(subscription)
            pending = self._pending.get(subscription)
            if queue is None or pending is None:
                return
            queued = list(queue._queue)
            queued.extend(pending)
            self._clear_queue(queue)
            pending.clear()
            initial = {"kind": "reset", "status": status, "lines": list(lines[-200:])}
            queue.put_nowait(initial)
            for event in queued:
                if event is _CLOSED:
                    continue
                if event.get("kind") == "reset":
                    # Keep the initial reset while preserving a restart reset
                    # that was generated during the tail read.
                    self._clear_queue(queue)
                    queue.put_nowait(initial)
                    queue.put_nowait(event)
                else:
                    self._queue_event(queue, event)

    async def unsubscribe(self, subscription: LogSubscription) -> None:
        with self._lock:
            queue = self._subscribers.pop(subscription, None)
            self._pending.pop(subscription, None)
            self._scheduled.discard(subscription)
            if queue is None:
                return
            self._clear_queue(queue)
            queue.put_nowait(_CLOSED)

    def _schedule(self, source: str, event: dict[str, Any]) -> None:
        callbacks: list[tuple[asyncio.AbstractEventLoop, LogSubscription]] = []
        with self._lock:
            for subscription in tuple(self._subscribers):
                if subscription.source != source:
                    continue
                pending = self._pending.setdefault(subscription, deque())
                self._stage_event(pending, event)
                loop = getattr(subscription, "_loop", None)
                if loop is None or loop.is_closed() or subscription in self._scheduled:
                    continue
                self._scheduled.add(subscription)
                callbacks.append((loop, subscription))
        for loop, subscription in callbacks:
            try:
                loop.call_soon_threadsafe(self._drain, subscription)
            except RuntimeError:
                with self._lock:
                    self._scheduled.discard(subscription)

    def _drain(self, subscription: LogSubscription) -> None:
        with self._lock:
            queue = self._subscribers.get(subscription)
            pending = self._pending.get(subscription)
            if queue is None or pending is None:
                self._scheduled.discard(subscription)
                return
            while pending:
                self._queue_event(queue, pending.popleft())
            self._scheduled.discard(subscription)
    def _stage_event(self, pending: deque[dict[str, Any]], event: dict[str, Any]) -> None:
        if event.get("kind") == "reset":
            pending.clear()
            pending.append(event)
            return
        while len(pending) >= self.queue_size:
            index = self._drop_index(pending, event.get("kind"))
            if index is None:
                return
            del pending[index]
        pending.append(event)

    @staticmethod
    def _drop_index(events: Any, incoming_kind: Any) -> int | None:
        # Data never evicts reset/status controls.  A status update may replace
        # an older status when no data remains, while a line is simply dropped.
        preferred = ("line",) if incoming_kind == "line" else ("line", "status")
        for kind in preferred:
            for index, item in enumerate(events):
                if item is not _CLOSED and item.get("kind") == kind:
                    return index
        return None

    @staticmethod
    def _clear_queue(queue: asyncio.Queue[Any]) -> None:
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    @classmethod
    def _queue_event(cls, queue: asyncio.Queue[Any], event: dict[str, Any]) -> None:
        if event.get("kind") == "reset":
            cls._clear_queue(queue)
            queue.put_nowait(event)
            return
        try:
            queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        items: list[Any] = []
        while True:
            try:
                items.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        index = cls._drop_index(items, event.get("kind"))
        if index is None:
            for item in items:
                queue.put_nowait(item)
            return
        del items[index]
        for item in items:
            queue.put_nowait(item)
        queue.put_nowait(event)

    @staticmethod
    def tail_file(path: Path, limit: int = 200, end: int | None = None) -> list[str]:
        """Read a bounded suffix, optionally stopping at a registration cutoff."""
        if limit < 1:
            return []
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                file_end = handle.tell()
                if end is None:
                    end = file_end
                else:
                    end = min(max(0, end), file_end)
                start = max(0, end - LogBroker.TAIL_BYTES)
                handle.seek(start)
                data = handle.read(end - start)
        except OSError:
            return []
        if start > 0:
            boundary = data.find(b"\n")
            if 0 <= boundary < len(data) - 1:
                data = data[boundary + 1 :]
        lines = data.splitlines()[-limit:]
        return [line.decode("utf-8", "replace") for line in lines]

    async def aclose(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subscribers = tuple(self._subscribers)
            self._subscribers.clear()
            self._pending.clear()
            self._scheduled.clear()
            for subscription in subscribers:
                subscription._closed = True
                queue = subscription._queue
                self._clear_queue(queue)
                queue.put_nowait(_CLOSED)

class ProcessLogPump:
    """Drain one merged child stdout/stderr pipe on a dedicated reader thread."""

    MAX_PARTIAL_LINE_BYTES = 64 * 1024
    EXIT_DRAIN_MAX_S = 0.25
    EXIT_DRAIN_QUIESCENCE_S = 0.05

    def __init__(self, process: Any, source: str, broker: LogBroker, log_path: Path) -> None:
        self.process = process
        self.source = source
        self.broker = broker
        self.log_path = log_path
        self._file: TextIO | None = None
        self._file_lock = threading.Lock()
        self._stream_close_lock = threading.Lock()
        self._stream_closed = False
        self._done = threading.Event()
        self._stop_requested = threading.Event()
        self._drain_after_exit = threading.Event()
        self._exit_drain_deadline = 0.0
        self.max_pending_bytes = 0
        self.max_published_line_bytes = 0
        self._thread: threading.Thread | None = None
        self._open_file()
        self.broker.set_file(source, log_path)
        self.broker.set_status(source, "live")
        self._thread = threading.Thread(target=self._drain, name=f"reachy-log-{source}", daemon=False)
        self._thread.start()

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    @property
    def file_handle(self) -> TextIO | None:
        return self._file

    def _open_file(self) -> None:
        with self.broker._lock:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self._file = self.log_path.open("w", encoding="utf-8", buffering=1)
            except OSError:
                self._file = None

    @staticmethod
    def _stream_fd(stream: Any) -> int | None:
        try:
            fd = stream.fileno()
        except (AttributeError, OSError, ValueError):
            return None
        return fd if type(fd) is int and fd >= 0 else None

    @classmethod
    def _bounded_prefix(cls, text: str) -> tuple[str, str]:
        encoded = text.encode("utf-8", "replace")
        if len(encoded) <= cls.MAX_PARTIAL_LINE_BYTES:
            return text, ""
        prefix_bytes = encoded[: cls.MAX_PARTIAL_LINE_BYTES]
        prefix = prefix_bytes.decode("utf-8", "ignore")
        consumed = len(prefix.encode("utf-8"))
        if consumed == 0:
            prefix = text[0]
            consumed = len(prefix.encode("utf-8"))
        remainder = encoded[consumed:].decode("utf-8", "replace")
        return prefix, remainder

    def _flush_oversized_pending(self, pending: str) -> str:
        while len(pending.encode("utf-8", "replace")) > self.MAX_PARTIAL_LINE_BYTES:
            chunk, pending = self._bounded_prefix(pending)
            self._write_line(chunk)
        self.max_pending_bytes = max(
            self.max_pending_bytes, len(pending.encode("utf-8", "replace"))
        )
        return pending

    def _write_bounded_text(self, text: str) -> None:
        pending = text.rstrip("\r\n")
        while pending:
            chunk, pending = self._bounded_prefix(pending)
            self._write_line(chunk)

    def _write_line(self, text: str) -> None:
        self.max_published_line_bytes = max(
            self.max_published_line_bytes, len(text.encode("utf-8", "replace"))
        )
        with self.broker._lock:
            with self._file_lock:
                if self._file is not None:
                    try:
                        self._file.write(text + "\n")
                        self._file.flush()
                    except (OSError, TypeError):
                        pass
            self.broker.publish(self.source, text)

    def _close_stream(self, stream: Any) -> None:
        with self._stream_close_lock:
            if self._stream_closed:
                return
            self._stream_closed = True
        try:
            stream.close()
        except Exception:
            pass

    def _drain_text_stream(self, stream: Any) -> None:
        """Fallback for file-like test doubles that do not expose a descriptor."""
        while not self._stop_requested.is_set():
            try:
                line = stream.readline()
            except Exception:
                return
            if line == "" or not isinstance(line, str):
                return
            self._write_bounded_text(line)

    def _drain(self) -> None:
        stream = getattr(self.process, "stdout", None)
        try:
            if stream is None:
                return
            fd = self._stream_fd(stream)
            if fd is None:
                self._drain_text_stream(stream)
                return

            # TextIOWrapper.readline() can stay blocked forever when a descendant
            # inherited the parent's pipe. Read the descriptor directly in bounded,
            # nonblocking chunks so stop_event, not EOF, controls exit.
            try:
                os.set_blocking(fd, False)
            except OSError:
                pass
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            pending = ""
            quiet_since: float | None = None
            while not self._stop_requested.is_set():
                draining_exit = self._drain_after_exit.is_set()
                if draining_exit:
                    remaining = self._exit_drain_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    select_timeout = min(0.05, remaining)
                else:
                    select_timeout = 0.05
                try:
                    readable, _, _ = select.select((fd,), (), (), select_timeout)
                except (OSError, ValueError):
                    break
                if not readable:
                    if draining_exit:
                        if quiet_since is None:
                            quiet_since = time.monotonic()
                        if time.monotonic() - quiet_since >= self.EXIT_DRAIN_QUIESCENCE_S:
                            break
                    continue
                quiet_since = None
                try:
                    chunk = os.read(fd, 64 * 1024)
                except BlockingIOError:
                    continue
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                pending += decoder.decode(chunk)
                pending = self._flush_oversized_pending(pending)
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    self._write_line(line.rstrip("\r"))
                    pending = self._flush_oversized_pending(pending)
            pending += decoder.decode(b"", final=True)
            pending = self._flush_oversized_pending(pending)
            if pending:
                self._write_line(pending.rstrip("\r\n"))
        finally:
            if stream is not None:
                self._close_stream(stream)
            with self._file_lock:
                if self._file is not None:
                    try:
                        self._file.flush()
                        self._file.close()
                    except OSError:
                        pass
                    self._file = None
            self._done.set()
            if not self._stop_requested.is_set():
                self.broker.set_status(self.source, "stopped")

    def stop(self, timeout: float = 5.0, parent_exited: bool = False) -> None:
        """Stop the reader, draining an exited parent’s ready pipe bytes first."""
        thread = self._thread
        if parent_exited:
            budget = min(max(0.0, timeout), self.EXIT_DRAIN_MAX_S)
            self._exit_drain_deadline = time.monotonic() + budget
            self._drain_after_exit.set()
        else:
            budget = max(0.0, timeout)
            self._stop_requested.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=budget)
        if parent_exited and thread is not None and thread.is_alive():
            # The descriptor loop is nonblocking; do not let an unexpected
            # reader stall turn an exited child into an unbounded shutdown.
            self._stop_requested.set()
        if thread is None or not thread.is_alive():
            self.broker.set_status(self.source, "stopped")

    @property
    def done(self) -> bool:
        return self._done.is_set()


class ControllerLogHandler(logging.Handler):
    """Forward controller records to a broker and a bounded-line file."""

    def __init__(self, broker: LogBroker, file_path: Path | None = None) -> None:
        super().__init__()
        self.broker = broker
        self.file_path = file_path
        self._file: TextIO | None = None
        self._lock = threading.Lock()
        if file_path is not None:
            try:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                self._file = file_path.open("a", encoding="utf-8", buffering=1)
            except OSError:
                self._file = None
            broker.set_file("controller", file_path)
        broker.bind_source("controller", status="live", file_path=file_path)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            timestamp = time.strftime("%H:%M:%S", time.localtime(record.created))
            message = f"{timestamp} {record.levelname:<8} {record.name}: {record.getMessage()}"
            with self.broker._lock:
                with self._lock:
                    if self._file is not None:
                        self._file.write(message + "\n")
                        self._file.flush()
                self.broker.publish("controller", message)
        except Exception:
            # Logging must never recursively report its own transport/file failure.
            return

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                try:
                    self._file.flush()
                    self._file.close()
                except OSError:
                    pass
                self._file = None
        super().close()


__all__ = ["ControllerLogHandler", "LogBroker", "LogSubscription", "ProcessLogPump"]
