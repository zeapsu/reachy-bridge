from __future__ import annotations

import httpx
import pytest

import reachy_controller.main as main_module
from reachy_controller.daemon_proxy import DaemonProxy
from reachy_controller.main import create_app


class FakeProxy:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses

    async def get_json(self, path: str, **_: object) -> object:
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return response

    async def close(self) -> None:
        return None


def test_create_app_uses_renamed_daemon_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEMON_URL", "http://daemon.test:9000")
    monkeypatch.setenv("DAEMON_TIMEOUT", "4.5")

    app = create_app()

    assert app.state.daemon_proxy.base_url == "http://daemon.test:9000"
    assert app.state.daemon_proxy.timeout == 4.5


def test_main_uses_local_controller_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    settings: dict[str, object] = {}
    monkeypatch.delenv("CONTROLLER_HOST", raising=False)
    monkeypatch.delenv("CONTROLLER_PORT", raising=False)
    monkeypatch.delenv("CONTROLLER_LOG_LEVEL", raising=False)
    monkeypatch.setattr(
        main_module.uvicorn,
        "run",
        lambda *args, **kwargs: settings.update(app=args[0], **kwargs),
    )

    main_module.main()

    assert settings == {
        "app": "reachy_controller.main:app",
        "host": "127.0.0.1",
        "port": 7861,
        "log_level": "info",
    }


def test_main_uses_renamed_controller_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings: dict[str, object] = {}
    monkeypatch.setenv("CONTROLLER_HOST", "100.64.0.1")
    monkeypatch.setenv("CONTROLLER_PORT", "9001")
    monkeypatch.setenv("CONTROLLER_LOG_LEVEL", "debug")
    monkeypatch.setattr(
        main_module.uvicorn,
        "run",
        lambda *args, **kwargs: settings.update(app=args[0], **kwargs),
    )

    main_module.main()

    assert settings == {
        "app": "reachy_controller.main:app",
        "host": "100.64.0.1",
        "port": 9001,
        "log_level": "debug",
    }


@pytest.mark.asyncio
async def test_daemon_proxy_forwards_to_daemon_api() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ready": True})

    proxy = DaemonProxy(
        "http://daemon.local:8000/",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await proxy.get_json("state/full", params={"with_doa": "true"})
    finally:
        await proxy.close()

    assert result == {"ready": True}
    assert seen[0].url == "http://daemon.local:8000/api/state/full?with_doa=true"


@pytest.mark.asyncio
async def test_telemetry_combines_daemon_and_host_state() -> None:
    proxy = FakeProxy(
        {
            "daemon/status": {"ready": True},
            "state/full": {
                "head_pose": [0.0, 0.0, 0.0, 0.1, 0.2, 0.3],
                "body_yaw": 0.4,
                "antennas_position": [0.5, 0.6],
                "doa": {"angle": 0.7, "speech_detected": True},
            },
            "apps/current-app-status": {
                "info": {"name": "reachy_mini_radio", "source_kind": "local"},
                "state": "running",
            },
            "motors/status": {"mode": "enabled"},
            "volume/current": {"volume": 80},
            "volume/microphone/current": {"volume": 60},
        }
    )
    app = create_app(proxy=proxy)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/telemetry")

    assert response.status_code == 200
    payload = response.json()
    assert payload["daemon"] == {"online": True, "status": {"ready": True}}
    assert payload["robot"]["state"]["body_yaw"] == 0.4
    assert payload["robot"]["motor_mode"] == "enabled"
    assert payload["app"]["info"]["name"] == "reachy_mini_radio"
    assert payload["audio"]["speaker_volume"] == 80
    assert payload["audio"]["microphone_volume"] == 60
    assert payload["host"]["hostname"]


@pytest.mark.asyncio
async def test_telemetry_stays_available_when_daemon_is_offline() -> None:
    error = httpx.ConnectError("offline")
    proxy = FakeProxy(
        {
            "daemon/status": error,
            "state/full": error,
            "apps/current-app-status": error,
            "motors/status": error,
            "volume/current": error,
            "volume/microphone/current": error,
        }
    )
    app = create_app(proxy=proxy)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/telemetry")

    assert response.status_code == 200
    assert response.json()["daemon"]["online"] is False


@pytest.mark.asyncio
async def test_root_serves_controller_ui() -> None:
    app = create_app(proxy=FakeProxy({}))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "reachy·controller" in response.text
    assert "/static/style.css" in response.text
    assert "/static/main.js" in response.text


def test_static_ui_wires_liquid_glass_to_live_controller_endpoints() -> None:
    static_dir = main_module.STATIC_DIR
    index = (static_dir / "index.html").read_text(encoding="utf-8")
    script = (static_dir / "main.js").read_text(encoding="utf-8")
    styles = (static_dir / "style.css").read_text(encoding="utf-8")

    assert "class=\"glass" in index
    assert "id=\"vp-switch\"" in index
    assert "id=\"camera-stream\"" in index
    camera_layer = index.split('<div class="vp-layer" data-mode="camera">', 1)[1].split(
        "</div>", 1
    )[0]
    assert '<canvas id="cam"' not in camera_layer
    assert camera_layer.index('id="camera-stream"') < camera_layer.index(
        'id="camera-placeholder"'
    )
    assert ".glass::before" in styles
    assert ".vp-switch" in styles
    assert "/telemetry/state-stream" in script
    assert "/api/system/stats-stream" in script
    assert "/api/audio/levels-stream" in script
    assert "/api/camera/availability" in script
    assert "/api/apps/start/" in script
    assert "/api/apps/stop/" in script
    assert "/api/apps/favorites" in script
    assert "/api/volume/set" in script
    assert "/api/volume/microphone/set" in script
    assert "/api/motors/set_mode/" in script




@pytest.mark.asyncio
async def test_served_theme_swatch_is_token_driven_for_all_themes() -> None:
    app = create_app(proxy=FakeProxy({}))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        index = await client.get("/")
        styles = await client.get("/static/style.css")

    assert index.status_code == 200
    assert styles.status_code == 200
    assert 'class="cur"' in index.text
    rule = styles.text.split(".theme-btn .cur", 1)[1].split("}", 1)[0]
    assert "background: var(--amber)" in rule
    assert "box-shadow: 0 0 8px 0 var(--amber)" in rule
    assert "transparent" not in rule
    for scheme in ("arctic", "emerald", "ultraviolet", "ember"):
        assert f'html[data-scheme="{scheme}"]' in styles.text

@pytest.mark.asyncio
async def test_index_includes_cache_control_no_cache_header() -> None:
    app = create_app(proxy=FakeProxy({}))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-cache"


@pytest.mark.asyncio
async def test_static_files_include_cache_control_no_cache_header() -> None:
    app = create_app(proxy=FakeProxy({}))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/static/main.js")

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-cache"


def test_apps_status_poll_has_non_overlapping_success_and_error_release() -> None:
    script = (
        main_module.STATIC_DIR / "main.js"
    ).read_text(encoding="utf-8")
    start = script.index("async function pollAppsStatus()")
    end = script.index("\n  async function performAppAction", start)
    poll = script[start:end]

    assert "if (state.appsStatusInflight) return;" in poll
    assert "state.appsStatusInflight = true;" in poll
    assert "finally" in poll
    assert "state.appsStatusInflight = false;" in poll
    assert poll.index("if (state.appsStatusInflight) return;") < poll.index(
        "await apiJson"
    )
    assert poll.index("state.appsStatusInflight = false;") > poll.index("finally")
