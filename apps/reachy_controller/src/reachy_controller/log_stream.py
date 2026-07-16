"""Bounded, thread-safe log fan-out and subprocess output pumps."""

from __future__ import annotations

import asyncio
import logging
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
        self._closed = False

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

    def __init__(self, *, ring_size: int = 500, queue_size: int = 256) -> None:
        if ring_size < 1 or queue_size < 1:
            raise ValueError("ring_size and queue_size must be positive")
        self.ring_size = ring_size
        self.queue_size = queue_size
        self._rings: dict[str, deque[str]] = {}
        self._statuses: dict[str, str] = {}
        self._files: dict[str, Path] = {}
        self._subscribers: dict[LogSubscription, asyncio.Queue[Any]] = {}
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

    async def subscribe(self, source: str, *, replay: list[str] | None = None) -> LogSubscription:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._closed:
                raise RuntimeError("log broker is closed")
            self._rings.setdefault(source, deque(maxlen=self.ring_size))
            status = self._statuses.get(source, "empty")
            lines = self.replay(source) if replay is None else list(replay[-200:])
            queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self.queue_size)
            subscription = LogSubscription(self, source, queue)
            self._subscribers[subscription] = queue
            queue.put_nowait({"kind": "reset", "status": status, "lines": lines})
            setattr(subscription, "_loop", loop)
            return subscription

    async def unsubscribe(self, subscription: LogSubscription) -> None:
        with self._lock:
            self._subscribers.pop(subscription, None)
            try:
                subscription._queue.put_nowait(_CLOSED)
            except asyncio.QueueFull:
                try:
                    subscription._queue.get_nowait()
                    subscription._queue.put_nowait(_CLOSED)
                except asyncio.QueueEmpty:
                    pass

    def _schedule(self, source: str, event: dict[str, Any]) -> None:
        with self._lock:
            loops = {
                loop
                for subscription in self._subscribers
                if subscription.source == source
                for loop in (getattr(subscription, "_loop", None),)
                if loop is not None and not loop.is_closed()
            }
        for loop in loops:
            try:
                loop.call_soon_threadsafe(self._fanout, source, event)
            except RuntimeError:
                continue

    def _fanout(self, source: str, event: dict[str, Any]) -> None:
        with self._lock:
            queues = tuple(
                queue for subscription, queue in self._subscribers.items() if subscription.source == source
            )
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    @staticmethod
    def tail_file(path: Path, limit: int = 200) -> list[str]:
        """Read only a bounded tail without loading an entire log into memory."""
        if limit < 1:
            return []
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                position = handle.tell()
                data = b""
                while position > 0 and data.count(b"\n") <= limit:
                    size = min(8192, position)
                    position -= size
                    handle.seek(position)
                    data = handle.read(size) + data
        except OSError:
            return []
        lines = data.splitlines()[-limit:]
        return [line.decode("utf-8", "replace") for line in lines]

    async def aclose(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subscribers = tuple(self._subscribers)
            self._subscribers.clear()
        for subscription in subscribers:
            subscription._closed = True
            try:
                subscription._queue.put_nowait(_CLOSED)
            except asyncio.QueueFull:
                try:
                    subscription._queue.get_nowait()
                    subscription._queue.put_nowait(_CLOSED)
                except asyncio.QueueEmpty:
                    pass


class ProcessLogPump:
    """Drain one merged child stdout/stderr pipe on a dedicated reader thread."""

    def __init__(self, process: Any, source: str, broker: LogBroker, log_path: Path) -> None:
        self.process = process
        self.source = source
        self.broker = broker
        self.log_path = log_path
        self._file: TextIO | None = None
        self._file_lock = threading.Lock()
        self._done = threading.Event()
        self._stop_requested = threading.Event()
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
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.log_path.open("w", encoding="utf-8", buffering=1)
        except OSError:
            self._file = None

    def _drain(self) -> None:
        stream = getattr(self.process, "stdout", None)
        try:
            if stream is None:
                return
            for line in iter(stream.readline, ""):
                if line == "" or not isinstance(line, str):
                    break
                text = line.rstrip("\r\n")
                with self._file_lock:
                    if self._file is not None:
                        try:
                            self._file.write(text + "\n")
                            self._file.flush()
                        except (OSError, TypeError):
                            pass
                self.broker.publish(self.source, text)
        finally:
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
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

    def stop(self, timeout: float = 5.0) -> None:
        """Close and join the reader; callers in async code must use ``to_thread``."""
        self._stop_requested.set()
        stream = getattr(self.process, "stdout", None)
        process_alive = True
        try:
            process_alive = self.process.poll() is None
        except (AttributeError, TypeError):
            pass
        if process_alive:
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            if process_alive:
                thread.join(timeout=timeout)
            else:
                thread.join()
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
