from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

import reachy_hermes.gateway as gateway_module
from reachy_hermes.gateway import (
    HermesGatewayClient,
    HermesRuntimeMismatch,
    verify_hermes_runtime,
)
from reachy_hermes.runtime_pin import RUNTIME_PIN, describe


def version_output(
    *,
    version: str = "0.19.0",
    build_date: str = "2026.7.20",
    upstream_sha: str = "e1367b44",
    local_sha: str | None = "15dc65ee",
) -> bytes:
    local = f" · local {local_sha} (+2 carried commits)" if local_sha else ""
    return (
        f"Hermes Agent v{version} ({build_date}) · upstream {upstream_sha}{local}\n"
        "Install directory: /opt/hermes-agent\n"
    ).encode()


def runner(output: bytes):
    async def run(
        _: Path, *, environment: dict[str, str] | None = None
    ) -> tuple[int, bytes, bytes]:
        del environment
        return 0, output, b""

    return run


@pytest.mark.asyncio
async def test_runtime_pin_match_passes() -> None:
    identity = await verify_hermes_runtime(
        Path("/opt/hermes"),
        version_runner=runner(version_output()),
    )
    assert identity["version"] == RUNTIME_PIN["version"]
    assert describe()["plugins"] == ["reachy_probe"]
    assert describe()["model_profile"] == {
        "provider": "unrecorded",
        "model": "unrecorded",
        "delivery_mode": "unrecorded",
    }


@pytest.mark.asyncio
async def test_runtime_version_mismatch_names_expected_and_found() -> None:
    with pytest.raises(HermesRuntimeMismatch) as raised:
        await verify_hermes_runtime(
            Path("/opt/hermes"),
            version_runner=runner(version_output(version="0.18.2")),
        )
    message = str(raised.value)
    assert "'version': '0.19.0'" in message
    assert "'version': '0.18.2'" in message


@pytest.mark.asyncio
async def test_sha_is_enforced_only_when_recorded_in_pin() -> None:
    unpinned_sha = {**RUNTIME_PIN, "upstream_sha": None, "local_sha": None}
    await verify_hermes_runtime(
        Path("/opt/hermes"),
        pin=unpinned_sha,
        version_runner=runner(version_output(upstream_sha="deadbeef", local_sha=None)),
    )

    pinned_sha = {**unpinned_sha, "upstream_sha": "cafebabe"}
    with pytest.raises(HermesRuntimeMismatch, match="cafebabe.*deadbeef"):
        await verify_hermes_runtime(
            Path("/opt/hermes"),
            pin=pinned_sha,
            version_runner=runner(version_output(upstream_sha="deadbeef")),
        )


@pytest.mark.asyncio
async def test_build_date_is_recorded_not_enforced() -> None:
    # A binary whose version matches the pin but reports a different build date
    # verifies without raising; build_date is documentation, not a gate.
    identity = await verify_hermes_runtime(
        Path("/opt/hermes"),
        version_runner=runner(version_output(build_date="2026.8.01")),
    )
    assert identity["version"] == RUNTIME_PIN["version"]
    assert identity["build_date"] == "2026.8.01"
    assert RUNTIME_PIN["build_date"] == "2026.7.20"


@pytest.mark.asyncio
async def test_allow_unpinned_escape_hatch_warns_and_returns_found_identity(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("REACHY_HERMES_ALLOW_UNPINNED", "1")
    with caplog.at_level(logging.WARNING):
        identity = await verify_hermes_runtime(
            Path("/opt/hermes"),
            version_runner=runner(version_output(version="0.18.2")),
        )
    assert identity["version"] == "0.18.2"
    assert "UNPINNED HERMES RUNTIME ALLOWED" in caplog.text


class FakeStream:
    async def readline(self) -> bytes:
        return b"HERMES_BACKEND_READY port=9120\n"

    async def read(self, _: int = -1) -> bytes:
        return b""


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = FakeStream()
        self.stderr = FakeStream()
        self.returncode: int | None = None
        self.pid = 12345


@pytest.mark.asyncio
async def test_gateway_spawn_uses_version_scoped_hermes_home_without_real_state(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "hermes"
    binary.touch()
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def spawn(*args: Any, **kwargs: Any) -> FakeProcess:
        calls.append((args, kwargs))
        return FakeProcess()

    stages: list[str] = []
    client = HermesGatewayClient(
        hermes_binary=binary,
        process_factory=spawn,
        state_root=tmp_path / "reachy-state",
        version_runner=runner(version_output()),
        stage_callback=stages.append,
    )
    await client.start()

    _, kwargs = calls[0]
    expected_root = tmp_path / "reachy-state" / "v0.19.0"
    assert kwargs["env"]["HERMES_HOME"] == str(expected_root / "home")
    assert client.runtime_dir == expected_root / "session"
    assert stages == ["verifying-hermes-runtime", "starting-gateway"]
    constructed = [kwargs["env"]["HERMES_HOME"], str(client.runtime_dir)]
    assert all(".hermes" not in path for path in constructed)

    client._process.returncode = 0
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_production_version_preflight_uses_scratch_hermes_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "hermes"
    binary.touch()
    environments: list[dict[str, str] | None] = []

    async def fake_run_version(
        _: Path, *, environment: dict[str, str] | None = None
    ) -> tuple[int, bytes, bytes]:
        environments.append(environment)
        return 0, version_output(), b""

    monkeypatch.setattr(gateway_module, "_run_version", fake_run_version)
    client = HermesGatewayClient(
        hermes_binary=binary,
        state_root=tmp_path / "reachy-state",
    )

    await client.verify_runtime()

    assert environments[0] is not None
    assert environments[0]["HERMES_HOME"] == str(
        tmp_path / "reachy-state" / "preflight" / "home"
    )
    assert ".hermes" not in environments[0]["HERMES_HOME"]
