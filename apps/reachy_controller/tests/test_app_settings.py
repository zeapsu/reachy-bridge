from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from reachy_controller.main import create_app


class FakeDaemonProxy:
    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _build_app(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Any:
    return create_app(
        proxy=FakeDaemonProxy(),
        settings_transport=httpx.MockTransport(handler),
    )


async def _request(
    app: Any,
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://controller.test",
        ) as client:
            return await client.request(method, path, **kwargs)
    finally:
        await app.state.app_settings_proxy.close()


def _schema_handler(request: httpx.Request) -> httpx.Response:
    payloads = {
        "/status": {"backend_provider": "openai_realtime", "has_openai_realtime_key": True},
        "/voices": ["cedar", "marin"],
        "/voices/current": {"voice": "cedar"},
    }
    return httpx.Response(200, json=payloads[request.url.path], request=request)


@pytest.mark.asyncio
async def test_get_settings_composes_upstream_schema_without_secret_value() -> None:
    app = _build_app(_schema_handler)

    response = await _request(app, "GET", "/api/apps/reachy_chat/settings")

    assert response.status_code == 200
    assert response.json() == {
        "app": "reachy_chat",
        "title": "Reachy Chat",
        "fields": [
            {
                "key": "api_key",
                "label": "OpenAI API key",
                "type": "password",
                "configured": True,
            },
            {
                "key": "voice",
                "label": "Voice",
                "type": "select",
                "value": "cedar",
                "options": ["cedar", "marin"],
            },
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "status_key", "label"),
    [
        ("openai", "has_openrouter_key", "OpenRouter API key"),
        ("gemini", "has_gemini_key", "Gemini API key"),
    ],
)
async def test_get_settings_uses_backend_credential_field(
    backend: str,
    status_key: str,
    label: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payloads = {
            "/status": {"backend_provider": backend, status_key: True},
            "/voices": ["Aoede"],
            "/voices/current": {"voice": "Aoede"},
        }
        return httpx.Response(200, json=payloads[request.url.path], request=request)

    app = _build_app(handler)
    response = await _request(app, "GET", "/api/apps/reachy_chat/settings")

    assert response.status_code == 200
    assert response.json()["fields"][0] == {
        "key": "api_key",
        "label": label,
        "type": "password",
        "configured": True,
    }


@pytest.mark.asyncio
async def test_huggingface_settings_omit_credential_field() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payloads = {
            "/status": {"backend_provider": "huggingface"},
            "/voices": ["Aiden"],
            "/voices/current": {"voice": "Aiden"},
        }
        return httpx.Response(200, json=payloads[request.url.path], request=request)

    app = _build_app(handler)
    response = await _request(app, "GET", "/api/apps/reachy_chat/settings")

    assert response.status_code == 200
    assert response.json()["fields"] == [
        {
            "key": "voice",
            "label": "Voice",
            "type": "select",
            "value": "Aiden",
            "options": ["Aiden"],
        }
    ]


@pytest.mark.asyncio
async def test_key_setting_reads_backend_then_saves_without_echoing_secret() -> None:
    seen: list[tuple[str, str, dict[str, str] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen.append((request.method, request.url.path, json.loads(body) if body else None))
        if request.url.path == "/status":
            return httpx.Response(
                200,
                json={"backend_provider": "openai_realtime", "has_openai_realtime_key": False},
                request=request,
            )
        if request.url.path == "/backend_config":
            return httpx.Response(200, json={"ok": True, "api_key": "sk-secret"}, request=request)
        raise AssertionError(request.url.path)

    app = _build_app(handler)
    response = await _request(
        app,
        "POST",
        "/api/apps/reachy_chat/settings/api_key",
        json={"value": "sk-secret"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "sk-secret" not in response.text
    assert seen == [
        ("GET", "/status", None),
        (
            "POST",
            "/backend_config",
            {"backend": "openai_realtime", "api_key": "sk-secret"},
        ),
    ]


@pytest.mark.asyncio
async def test_voice_setting_proxies_json_body_and_upstream_response() -> None:
    seen: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen.append((request.method, request.url.path, body))
        return httpx.Response(
            200, json={"ok": True, "status": "Voice changed to cedar."}, request=request
        )

    app = _build_app(handler)
    response = await _request(
        app,
        "POST",
        "/api/apps/reachy_chat/settings/voice",
        json={"value": " cedar "},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "Voice changed to cedar."}
    assert seen == [("POST", "/voices/apply", b'{"voice":"cedar"}')]


@pytest.mark.asyncio
async def test_upstream_http_status_is_propagated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(418, json={"detail": "upstream failure"}, request=request)

    app = _build_app(handler)
    response = await _request(app, "GET", "/api/apps/reachy_chat/settings")

    assert response.status_code == 418
    assert response.json() == {"detail": "upstream failure"}


@pytest.mark.asyncio
async def test_unavailable_upstream_returns_honest_503() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    app = _build_app(handler)
    response = await _request(app, "GET", "/api/apps/reachy_chat/settings")

    assert response.status_code == 503
    assert response.json() == {"detail": "Reachy Chat settings service unavailable"}


@pytest.mark.asyncio
async def test_mid_response_protocol_failure_returns_honest_503() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("app stopped mid-response", request=request)

    app = _build_app(handler)
    response = await _request(app, "GET", "/api/apps/reachy_chat/settings")

    assert response.status_code == 503
    assert response.json() == {"detail": "Reachy Chat settings service unavailable"}


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["api_key", "voice"])
async def test_empty_setting_value_is_rejected_before_upstream_call(key: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    app = _build_app(handler)
    response = await _request(
        app,
        "POST",
        f"/api/apps/reachy_chat/settings/{key}",
        json={"value": "  "},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid request body"}
    assert calls == 0


@pytest.mark.asyncio
async def test_unknown_app_and_setting_return_404() -> None:
    app = _build_app(_schema_handler)
    unknown_app = await _request(app, "GET", "/api/apps/reachy_dance/settings")
    unknown_setting = await _request(
        app,
        "POST",
        "/api/apps/reachy_chat/settings/missing",
        json={"value": "x"},
    )

    assert unknown_app.status_code == 404
    assert unknown_setting.status_code == 404


@pytest.mark.asyncio
async def test_apps_status_uses_registry_for_has_settings() -> None:
    app = _build_app(_schema_handler)

    async def statuses() -> list[dict[str, Any]]:
        return [
            {"name": "reachy_chat", "status": "stopped"},
            {"name": "reachy_dance", "status": "stopped"},
        ]

    app.state.app_supervisor.aget_all_status = statuses
    response = await _request(app, "GET", "/api/apps/status")

    assert response.status_code == 200
    assert response.json() == [
        {"name": "reachy_chat", "status": "stopped", "has_settings": True},
        {"name": "reachy_dance", "status": "stopped", "has_settings": False},
    ]
