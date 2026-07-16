"""Controller, daemon, and managed-app log source discovery and SSE."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from reachy_controller.app_supervisor import AppSupervisor
from reachy_controller.log_stream import LogBroker, LogSubscription

router = APIRouter(prefix="/api/logs", tags=["logs"])
_KEEPALIVE_SECONDS = 15.0
_BATCH_SECONDS = 0.25
_BATCH_LINES = 32


def _source_ids(request: Request) -> list[str]:
    supervisor: AppSupervisor = request.app.state.app_supervisor
    return ["controller", "daemon", *supervisor.APPS]

def _status_for_source(request: Request, source: str) -> str:
    broker: LogBroker = request.app.state.log_broker
    status = broker.source_status(source)
    if source == "daemon":
        daemon = request.app.state.daemon_supervisor
        if getattr(daemon._daemon, "external", False):
            return "unavailable"
    if status == "empty":
        path = broker.source_file(source)
        if path is not None and path.exists():
            return "stopped"
    return status


@router.get("/sources")
async def log_sources(request: Request) -> dict[str, list[dict[str, Any]]]:
    """Return dynamic source ids and honest capture/status metadata."""
    supervisor: AppSupervisor = request.app.state.app_supervisor
    statuses = await asyncio.to_thread(supervisor.get_all_status)
    by_name = {item["name"]: item for item in statuses}
    sources: list[dict[str, Any]] = []
    for source in _source_ids(request):
        item: dict[str, Any] = {"id": source, "status": _status_for_source(request, source)}
        if source in by_name:
            app = by_name[source]
            item.update({"pid": app.get("pid"), "app_status": app.get("status"), "external": app.get("external", False)})
            if app.get("external"):
                item["status"] = "unavailable"
        sources.append(item)
    return {"sources": sources}


async def _stream_events(
    broker: LogBroker,
    subscription: LogSubscription,
) -> AsyncGenerator[str, None]:
    last_event = asyncio.get_running_loop().time()
    try:
        while True:
            remaining = _KEEPALIVE_SECONDS - (asyncio.get_running_loop().time() - last_event)
            try:
                event = await asyncio.wait_for(
                    subscription.__anext__(), timeout=max(remaining, 0.0)
                )
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                last_event = asyncio.get_running_loop().time()
                continue
            except StopAsyncIteration:
                return

            kind = event.get("kind")
            if kind == "reset":
                payload = {"status": event.get("status", "empty"), "lines": event.get("lines", [])}
                yield f"event: log-reset\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                last_event = asyncio.get_running_loop().time()
                continue
            if kind == "status":
                yield f"event: source-status\ndata: {json.dumps({'status': event.get('status', 'empty')})}\n\n"
                last_event = asyncio.get_running_loop().time()
                continue
            if kind != "line":
                continue

            lines = [str(event.get("line", ""))]
            deadline = asyncio.get_running_loop().time() + _BATCH_SECONDS
            control_event: dict[str, Any] | None = None
            while len(lines) < _BATCH_LINES:
                timeout = deadline - asyncio.get_running_loop().time()
                if timeout <= 0:
                    break
                try:
                    pending = await asyncio.wait_for(subscription.__anext__(), timeout=timeout)
                except asyncio.TimeoutError:
                    break
                except StopAsyncIteration:
                    break
                if pending.get("kind") != "line":
                    control_event = pending
                    break
                lines.append(str(pending.get("line", "")))
            # A control event terminates this batch, but only after all lines
            # read before it have been emitted.
            yield f"event: log-lines\ndata: {json.dumps({'lines': lines}, ensure_ascii=False)}\n\n"
            if control_event is not None:
                if control_event.get("kind") == "status":
                    yield f"event: source-status\ndata: {json.dumps({'status': control_event.get('status', 'empty')})}\n\n"
                elif control_event.get("kind") == "reset":
                    payload = {"status": control_event.get("status", "empty"), "lines": control_event.get("lines", [])}
                    yield f"event: log-reset\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            last_event = asyncio.get_running_loop().time()
    except asyncio.CancelledError:
        return
    finally:
        await subscription.aclose()


@router.get("/stream")
async def log_stream(request: Request, source: str = Query(...)) -> StreamingResponse:
    """Stream one validated log source as bounded, batched Server-Sent Events."""
    if source not in _source_ids(request):
        raise HTTPException(status_code=404, detail="Unknown log source")
    broker: LogBroker = request.app.state.log_broker
    initial_status = _status_for_source(request, source)
    # Register before the persisted tail read.  The broker captures the ring
    # snapshot/status under its lock and holds live events in bounded staging.
    subscription = await broker.subscribe(
        source,
        initial_status=initial_status,
        defer_initial=True,
    )
    replay = subscription._initial_lines
    if not replay:
        path = broker.source_file(source)
        if path is not None:
            replay = await asyncio.to_thread(broker.tail_file, path, 200)
    await broker.initialize(subscription, lines=replay, status=initial_status)
    return StreamingResponse(
        _stream_events(broker, subscription),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
