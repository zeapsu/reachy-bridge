"""Lazy controller-process broadcasters for shared daemon streams and stats."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable

from reachy_controller.jetson_stats import collect_jetson_stats

_MISSING = object()
_CLOSED = object()


class _Subscription:
    """A bounded async queue owned by one broadcaster subscriber."""

    def __init__(self, owner: _LazyBroadcaster, queue: asyncio.Queue[Any]) -> None:
        self._owner = owner
        self._queue = queue
        self._closed = False

    def __aiter__(self) -> _Subscription:
        return self

    async def __anext__(self) -> Any:
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
            await self._owner._unsubscribe(self)

    async def __aenter__(self) -> _Subscription:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


class _LazyBroadcaster:
    """Shared bounded fan-out lifecycle used by the two controller streams."""

    def __init__(self, *, queue_size: int) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._queue_size = queue_size
        self._subscribers: dict[_Subscription, asyncio.Queue[Any]] = {}
        self._latest: Any = _MISSING
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def latest(self) -> Any | None:
        return None if self._latest is _MISSING else self._latest

    @property
    def upstream_task(self) -> asyncio.Task[None] | None:
        task = self._task
        return task if task is not None and not task.done() else None

    async def subscribe(self) -> _Subscription:
        if self._closed:
            raise RuntimeError("broadcaster is closed")
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._queue_size)
        subscription = _Subscription(self, queue)
        self._subscribers[subscription] = queue
        if self._latest is not _MISSING:
            queue.put_nowait(self._latest)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        return subscription

    async def _unsubscribe(self, subscription: _Subscription) -> None:
        self._subscribers.pop(subscription, None)
        if self._subscribers or self._task is None:
            return
        task = self._task
        self._task = None
        if task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _publish(self, value: Any) -> None:
        """Publish a value, dropping the oldest queued value when necessary."""
        self._latest = value
        for queue in tuple(self._subscribers.values()):
            try:
                queue.put_nowait(value)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                queue.put_nowait(value)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        subscriptions = tuple(self._subscribers)
        self._subscribers.clear()
        for subscription in subscriptions:
            subscription._closed = True
            queue = subscription._queue
            while True:
                try:
                    queue.put_nowait(_CLOSED)
                    break
                except asyncio.QueueFull:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        raise NotImplementedError


class StateBroadcaster(_LazyBroadcaster):
    """Share one reconnecting full-state WebSocket among all consumers."""

    def __init__(
        self,
        proxy: Any,
        *,
        queue_size: int = 32,
        reconnect_delay: float = 1.0,
    ) -> None:
        super().__init__(queue_size=queue_size)
        self._proxy = proxy
        self._reconnect_delay = reconnect_delay

    async def _run(self) -> None:
        while self._subscribers:
            try:
                async for state in self._proxy.ws_stream_full_state():
                    if not self._subscribers:
                        return
                    self._publish(state)
            except asyncio.CancelledError:
                raise
            except Exception:
                # DaemonProxy normally turns transport failures into the same
                # sentinel. Keep fake/custom proxies on the same SSE contract.
                if self._subscribers:
                    self._publish({"error": "daemon_unavailable"})
            if self._subscribers:
                await asyncio.sleep(self._reconnect_delay)


class AudioLevelsBroadcaster(_LazyBroadcaster):
    """Share one fresh daemon microphone GET among all SSE consumers."""

    REQUIRED_FIELDS = frozenset(
        {
            "rms",
            "rms_db",
            "low",
            "mid",
            "high",
            "sample_rate_hz",
            "source",
            "timestamp",
        }
    )

    def __init__(
        self,
        proxy: Any,
        *,
        interval: float = 0.1,
        reconnect_delay: float = 1.0,
        queue_size: int = 8,
    ) -> None:
        super().__init__(queue_size=queue_size)
        if interval <= 0 or reconnect_delay <= 0:
            raise ValueError("audio broadcaster intervals must be positive")
        self._proxy = proxy
        self._interval = interval
        self._reconnect_delay = reconnect_delay

    async def _unsubscribe(self, subscription: _Subscription) -> None:
        if subscription in self._subscribers and len(self._subscribers) == 1:
            self._latest = _MISSING
        await super()._unsubscribe(subscription)

    async def aclose(self) -> None:
        self._latest = _MISSING
        await super().aclose()


    @staticmethod
    def _finite_number(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )

    @classmethod
    def _valid_levels(cls, value: Any) -> bool:
        if not isinstance(value, dict) or not cls.REQUIRED_FIELDS.issubset(value):
            return False

        for field in ("rms", "low", "mid", "high"):
            level = value[field]
            if not cls._finite_number(level) or not 0 <= level <= 1:
                return False
        rms_db = value["rms_db"]
        if not cls._finite_number(rms_db) or not -60 <= rms_db <= 0:
            return False
        sample_rate_hz = value["sample_rate_hz"]
        if not cls._finite_number(sample_rate_hz) or sample_rate_hz <= 0:
            return False
        source = value["source"]
        if not isinstance(source, str) or not source.strip():
            return False
        # The daemon contract uses an RFC 3339 UTC string. Keep the validator
        # strict about presence/content without reparsing a transport field.
        timestamp = value["timestamp"]
        return isinstance(timestamp, str) and bool(timestamp.strip())

    async def _run(self) -> None:
        while self._subscribers:
            try:
                levels = await self._proxy.get_audio_levels()
            except asyncio.CancelledError:
                raise
            except Exception:
                levels = None

            if self._valid_levels(levels):
                self._publish(levels)
                delay = self._interval
            else:
                self._publish({"error": "daemon_unavailable"})
                delay = self._reconnect_delay

            if self._subscribers:
                await asyncio.sleep(delay)


class StatsBroadcaster(_LazyBroadcaster):
    """Share daemon-gated Jetson samples on the existing 1.5-second cadence."""

    def __init__(
        self,
        proxy: Any,
        *,
        collect_stats: Callable[[], dict[str, Any]] | None = None,
        interval: float = 1.5,
        queue_size: int = 8,
    ) -> None:
        super().__init__(queue_size=queue_size)
        self._proxy = proxy
        self._collect_stats = collect_stats or collect_jetson_stats
        self._interval = interval

    async def _run(self) -> None:
        while self._subscribers:
            try:
                status = await self._proxy.get_daemon_status()
            except asyncio.CancelledError:
                raise
            except Exception:
                status = None

            if status is None:
                # None is an internal offline sample. Routes omit it while it
                # still updates latest-on-subscribe and keeps all clients in sync.
                self._publish(None)
            else:
                try:
                    stats = await asyncio.to_thread(self._collect_stats)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    stats = None
                if stats is not None:
                    self._publish(stats)

            if self._subscribers:
                await asyncio.sleep(self._interval)


__all__ = ["AudioLevelsBroadcaster", "StateBroadcaster", "StatsBroadcaster"]
