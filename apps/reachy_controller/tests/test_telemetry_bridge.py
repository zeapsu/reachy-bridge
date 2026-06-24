from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from reachy_controller.daemon_proxy import DaemonProxy
from reachy_controller.main import create_app
from reachy_controller.routes import telemetry as telemetry_module


class BridgeProxy:
    def __init__(self, state: dict | None, status: dict | None) -> None:
        self.state = state
        self.status = status

    async def get_full_state(self) -> dict | None:
        return self.state

    async def get_daemon_status(self) -> dict | None:
        return self.status

    async def ws_stream_full_state(self) -> AsyncIterator[dict]:
        yield {"head_pose": [0, 0, 0, 0, 0, 0]}
        yield {"error": "daemon_unavailable"}

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_daemon_proxy_snapshot_helpers_use_spec_paths() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ready": True})

    proxy = DaemonProxy("http://daemon.local:8000", transport=httpx.MockTransport(handler))
    try:
        state = await proxy.get_full_state()
        status = await proxy.get_daemon_status()
    finally:
        await proxy.close()

    assert state == {"ready": True}
    assert status == {"ready": True}
    assert [str(request.url) for request in seen] == [
        "http://daemon.local:8000/api/state/full?with_doa=true",
        "http://daemon.local:8000/api/daemon/status",
    ]


@pytest.mark.asyncio
async def test_daemon_proxy_snapshot_helpers_return_none_on_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    proxy = DaemonProxy("http://daemon.local:8000", transport=httpx.MockTransport(handler))
    try:
        assert await proxy.get_full_state() is None
        assert await proxy.get_daemon_status() is None
    finally:
        await proxy.close()


@pytest.mark.asyncio
async def test_daemon_proxy_state_stream_requests_doa(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_urls: list[str] = []

    class FakeWebSocket:
        async def __aenter__(self) -> "FakeWebSocket":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def __aiter__(self) -> AsyncIterator[str]:
            yield '{"doa": {"angle": 1.57, "speech_detected": true}}'

    def fake_connect(url: str, *_: Any, **__: Any) -> FakeWebSocket:
        seen_urls.append(url)
        return FakeWebSocket()

    monkeypatch.setattr("reachy_controller.daemon_proxy.websockets.connect", fake_connect)
    proxy = DaemonProxy("http://daemon.local:8000")

    events = [event async for event in proxy.ws_stream_full_state()]

    assert events == [{"doa": {"angle": 1.57, "speech_detected": True}}]
    assert seen_urls == ["ws://daemon.local:8000/api/state/ws/full?with_doa=true"]


@pytest.mark.asyncio
async def test_snapshot_routes_preserve_api_prefixes() -> None:
    app = create_app(proxy=BridgeProxy({"body_yaw": 0.2}, {"ready": True}))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        state = await client.get("/api/telemetry/state")
        status = await client.get("/api/telemetry/daemon-status")

    assert state.status_code == 200
    assert state.json() == {"body_yaw": 0.2}
    assert status.status_code == 200
    assert status.json() == {"ready": True}


@pytest.mark.asyncio
async def test_snapshot_routes_report_daemon_unavailable() -> None:
    app = create_app(proxy=BridgeProxy(None, None))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        state = await client.get("/api/telemetry/state")
        status = await client.get("/api/telemetry/daemon-status")

    assert state.status_code == 503
    assert status.status_code == 503


@pytest.mark.asyncio
async def test_sse_adapter_maps_daemon_messages_to_spec_events() -> None:
    proxy = BridgeProxy({}, {})

    events = [
        event
        async for event in telemetry_module._daemon_state_stream_generator(proxy)
    ]

    assert events == [
        'event: state-update\ndata: {"head_pose": [0, 0, 0, 0, 0, 0]}\n\n',
        "event: daemon-offline\ndata: {}\n\n",
    ]


def test_state_stream_uses_root_telemetry_path() -> None:
    """Verify /telemetry/state-stream is registered on root, not under /api."""
    from reachy_controller.routes.telemetry import root_router
    from reachy_controller.routes.telemetry import router as telemetry_router

    # Check that root_router has the state-stream endpoint
    root_paths = {route.path for route in root_router.routes if hasattr(route, "path")}
    api_paths = {route.path for route in telemetry_router.routes if hasattr(route, "path")}

    assert "/telemetry/state-stream" in root_paths
    assert "/telemetry/state-stream" not in api_paths


def test_jetson_stats_module_contains_host_metric_collection() -> None:
    module = importlib.import_module("reachy_controller.jetson_stats")

    assert set(module.collect_host_metrics()) == {
        "hostname",
        "cpu_percent",
        "gpu_percent",
        "temperature_c",
        "memory_used_gb",
        "memory_total_gb",
    }
