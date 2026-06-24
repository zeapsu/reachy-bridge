from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from reachy_controller.daemon_proxy import DaemonProxy
from reachy_controller.main import create_app


@pytest.mark.asyncio
async def test_daemon_proxy_control_helpers_use_expected_requests() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    proxy = DaemonProxy("http://daemon.local:8000", transport=httpx.MockTransport(handler))
    try:
        results = [
            await proxy.get_speaker_volume(),
            await proxy.set_speaker_volume(42),
            await proxy.get_microphone_volume(),
            await proxy.set_microphone_volume(37),
            await proxy.get_motor_status(),
            await proxy.set_motor_mode("gravity_compensation"),
            await proxy.enable_wobbling(),
            await proxy.disable_wobbling(),
            await proxy.play_sound("hello.wav"),
            await proxy.play_test_sound(),
        ]
    finally:
        await proxy.close()

    assert results == [{"ok": True}] * 10
    assert [(request.method, request.url.path) for request in seen] == [
        ("GET", "/api/volume/current"),
        ("POST", "/api/volume/set"),
        ("GET", "/api/volume/microphone/current"),
        ("POST", "/api/volume/microphone/set"),
        ("GET", "/api/motors/status"),
        ("POST", "/api/motors/set_mode/gravity_compensation"),
        ("POST", "/api/media/wobbling/enable"),
        ("POST", "/api/media/wobbling/disable"),
        ("POST", "/api/media/play_sound"),
        ("POST", "/api/volume/test-sound"),
    ]
    assert [json.loads(request.content) for request in (seen[1], seen[3], seen[8])] == [
        {"volume": 42},
        {"volume": 37},
        {"file": "hello.wav"},
    ]
    assert all(not request.content for request in (seen[0], seen[2], *seen[4:8], seen[9]))


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(200, content=b"bad")])
async def test_daemon_proxy_control_helpers_return_none_for_bad_responses(
    response: httpx.Response,
) -> None:
    proxy = DaemonProxy(
        "http://daemon.local:8000",
        transport=httpx.MockTransport(lambda _: response),
    )
    try:
        assert await proxy.get_speaker_volume() is None
    finally:
        await proxy.close()


class ControlProxy:
    def __init__(self, result: dict[str, object] | None = None) -> None:
        self.result = {"ok": True} if result is None else result
        self.get_speaker_volume = AsyncMock(return_value=self.result)
        self.set_speaker_volume = AsyncMock(return_value=self.result)
        self.get_microphone_volume = AsyncMock(return_value=self.result)
        self.set_microphone_volume = AsyncMock(return_value=self.result)
        self.get_motor_status = AsyncMock(return_value=self.result)
        self.set_motor_mode = AsyncMock(return_value=self.result)
        self.enable_wobbling = AsyncMock(return_value=self.result)
        self.disable_wobbling = AsyncMock(return_value=self.result)
        self.play_sound = AsyncMock(return_value=self.result)
        self.play_test_sound = AsyncMock(return_value=self.result)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_control_routes_delegate_and_forward_request_bodies() -> None:
    proxy = ControlProxy()
    app = create_app(proxy=proxy)  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = [
            await client.get("/api/volume/current"),
            await client.post("/api/volume/set", json={"volume": 42}),
            await client.get("/api/volume/microphone/current"),
            await client.post("/api/volume/microphone/set", json={"volume": 37}),
            await client.get("/api/motors/status"),
            await client.post("/api/motors/set_mode/gravity_compensation"),
            await client.post("/api/media/wobbling/enable"),
            await client.post("/api/media/wobbling/disable"),
            await client.post("/api/media/play_sound", json={"file": "hello.wav"}),
            await client.post("/api/volume/test-sound"),
        ]

    assert all(response.status_code == 200 for response in responses)
    assert all(response.json() == {"ok": True} for response in responses)
    proxy.set_speaker_volume.assert_awaited_once_with(42)
    proxy.set_microphone_volume.assert_awaited_once_with(37)
    proxy.set_motor_mode.assert_awaited_once_with("gravity_compensation")
    proxy.play_sound.assert_awaited_once_with("hello.wav")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/volume/current", None),
        ("POST", "/api/volume/set", {"volume": 42}),
        ("GET", "/api/volume/microphone/current", None),
        ("POST", "/api/volume/microphone/set", {"volume": 37}),
        ("GET", "/api/motors/status", None),
        ("POST", "/api/motors/set_mode/enabled", None),
        ("POST", "/api/media/wobbling/enable", None),
        ("POST", "/api/media/wobbling/disable", None),
        ("POST", "/api/media/play_sound", {"file": "hello.wav"}),
        ("POST", "/api/volume/test-sound", None),
    ],
)
async def test_control_routes_map_offline_to_json_503(
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    proxy = ControlProxy()
    for value in vars(proxy).values():
        if isinstance(value, AsyncMock):
            value.return_value = None
    app = create_app(proxy=proxy)  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.request(method, path, json=body)

    assert response.status_code == 503
    assert response.json() == {"detail": "Daemon unavailable"}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/volume/set", "/api/volume/microphone/set"])
@pytest.mark.parametrize("volume", [-1, 101, 50.5, "50", True])
async def test_volume_routes_reject_invalid_values(path: str, volume: object) -> None:
    proxy = ControlProxy()
    app = create_app(proxy=proxy)  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(path, json={"volume": volume})

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_motor_route_rejects_invalid_mode() -> None:
    proxy = ControlProxy()
    app = create_app(proxy=proxy)  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/motors/set_mode/gravity")

    assert response.status_code == 400
    proxy.set_motor_mode.assert_not_awaited()
