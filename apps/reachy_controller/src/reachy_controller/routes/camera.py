"""Controller-owned camera availability and streaming proxy routes."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from reachy_controller.app_supervisor import AppSupervisor
from reachy_controller.daemon_proxy import DaemonProxy

router = APIRouter(prefix="/api", tags=["camera"])
CAMERA_STREAM_STARTUP_TIMEOUT_S = 8.0


class CameraLease:
    """Idempotent handle for one dashboard upstream stream."""

    def __init__(self, manager: "DashboardCameraLeaseManager", token: int) -> None:
        self._manager = manager
        self._token = token
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._manager.release(self._token)


class DashboardCameraLeaseManager:
    """Track and close all controller-owned dashboard camera streams."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._next_token = 0
        self._active: dict[int, Callable[[], Awaitable[None]]] = {}

    @property
    def active_count(self) -> int:
        return len(self._active)

    async def acquire(self, close: Callable[[], Awaitable[None]]) -> CameraLease:
        async with self._lock:
            self._next_token += 1
            token = self._next_token
            self._active[token] = close
        return CameraLease(self, token)

    async def release(self, token: int) -> None:
        async with self._lock:
            self._active.pop(token, None)

    async def close_all(self) -> None:
        """Close every stream before a camera app takes ownership."""
        async with self._lock:
            closers = list(self._active.values())
            self._active.clear()
        if closers:
            await asyncio.gather(*(self._close_safely(close) for close in closers))

    @staticmethod
    async def _close_safely(close: Callable[[], Awaitable[None]]) -> None:
        with contextlib.suppress(Exception):
            await close()


@router.get("/camera/availability")
async def camera_availability(request: Request) -> dict[str, Any]:
    """Return whether the dashboard may take camera ownership."""
    supervisor: AppSupervisor = request.app.state.app_supervisor
    holder = supervisor.camera_holder()
    if holder is not None:
        return {"available": False, "reason": "in_use", "holder": holder}

    proxy: DaemonProxy = request.app.state.daemon_proxy
    media_status = await proxy.get_media_status()
    daemon_status = None
    if media_status is None:
        daemon_status = await proxy.get_daemon_status()

    if media_status is None and daemon_status is None:
        return {"available": False, "reason": "daemon_down", "holder": None}
    if media_status is not None and (
        media_status.get("available") is False or media_status.get("released") is True
    ):
        return {"available": False, "reason": "media_unavailable", "holder": None}

    return {
        "available": supervisor.camera_available(),
        "reason": "available",
        "holder": None,
    }


@router.get("/camera/specs")
async def camera_specs(request: Request) -> dict[str, Any]:
    """Return one-shot camera specs from the daemon."""
    proxy: DaemonProxy = request.app.state.daemon_proxy
    try:
        return await proxy.get_json("camera/specs")
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="Daemon unavailable") from None


@router.get("/camera/stream")
async def camera_stream(request: Request) -> Response:
    """Forward the daemon MJPEG stream while retaining controller ownership."""
    supervisor: AppSupervisor = request.app.state.app_supervisor
    holder = supervisor.camera_holder()
    if holder is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Camera is in use by {holder}; stop that app before opening the dashboard stream.",
        )

    proxy: DaemonProxy = request.app.state.daemon_proxy
    leases: DashboardCameraLeaseManager = request.app.state.camera_leases
    upstream_context: Any | None = None
    upstream: Any | None = None
    lease: CameraLease | None = None
    close_state = False

    async def close_upstream() -> None:
        nonlocal close_state
        if close_state:
            return
        close_state = True
        if upstream_context is not None:
            with contextlib.suppress(Exception):
                await upstream_context.__aexit__(None, None, None)

    async def release_resources() -> None:
        nonlocal lease
        await close_upstream()
        if lease is not None:
            await lease.release()
            lease = None

    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CAMERA_STREAM_STARTUP_TIMEOUT_S

        def remaining_startup() -> float:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            return remaining

        upstream_context = proxy.stream(
            "GET",
            "camera/stream.mjpg",
            headers={"accept": "multipart/x-mixed-replace"},
        )
        upstream = await asyncio.wait_for(
            upstream_context.__aenter__(), remaining_startup()
        )
        content_type = upstream.headers.get("content-type", "")
        if upstream.status_code >= 400:
            content = await asyncio.wait_for(upstream.aread(), remaining_startup())
            await close_upstream()
            return Response(
                content=content,
                status_code=503 if upstream.status_code >= 500 else upstream.status_code,
                headers={"content-type": content_type} if content_type else None,
            )

        lease = await leases.acquire(close_upstream)
        holder = supervisor.camera_holder()
        if holder is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Camera is in use by {holder}; stop that app before opening the dashboard stream.",
            )

        iterator: AsyncIterator[bytes] = upstream.aiter_raw()
        first_chunk = await asyncio.wait_for(
            iterator.__anext__(), remaining_startup()
        )
        holder = supervisor.camera_holder()
        if holder is not None or close_state:
            raise HTTPException(
                status_code=409 if holder is not None else 503,
                detail=(
                    f"Camera is in use by {holder}; stop that app before opening the dashboard stream."
                    if holder is not None
                    else "Camera upstream closed during startup"
                ),
            )
    except asyncio.CancelledError:
        await release_resources()
        raise
    except HTTPException:
        await release_resources()
        raise
    except (httpx.HTTPError, OSError, RuntimeError, StopAsyncIteration, asyncio.TimeoutError):
        await release_resources()
        return Response(
            content='{"detail":"Camera upstream failed while opening the stream"}',
            status_code=503,
            media_type="application/json",
        )

    assert upstream is not None
    assert lease is not None

    async def chunks() -> AsyncIterator[bytes]:
        try:
            yield first_chunk
            async for chunk in iterator:
                yield chunk
        except asyncio.CancelledError:
            raise
        finally:
            await release_resources()

    return StreamingResponse(
        chunks(),
        status_code=upstream.status_code,
        headers={"content-type": content_type} if content_type else {},
    )
