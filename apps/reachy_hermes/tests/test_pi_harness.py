from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from reachy_hermes.pi_harness import (
    PI_CLI_PATH_ENV,
    PI_CONFIG_PATH_ENV,
    PI_NODE_PATH_ENV,
    PI_RUNTIME_PACKAGE,
    PI_RUNTIME_VERSION,
    PI_TOOL_INVENTORY_NAME,
    PI_TOOL_PROMPT_NAME,
    PiConfigError,
    PiHarnessError,
    PiRealtimeHarness,
    PiRuntime,
    realtime_functions,
    resolve_pi_config,
)


def _runtime_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    agent_dir = tmp_path / "pi" / "agent"
    package_dir = (
        agent_dir / "npm" / "node_modules" / "@mariozechner" / "pi-coding-agent"
    )
    (package_dir / "dist").mkdir(parents=True)
    (package_dir / "dist" / "cli.js").write_text("// test CLI", encoding="utf-8")
    (package_dir / "package.json").write_text(
        json.dumps({"name": PI_RUNTIME_PACKAGE, "version": PI_RUNTIME_VERSION}),
        encoding="utf-8",
    )
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\n", encoding="utf-8")
    return {PI_CONFIG_PATH_ENV: str(agent_dir), PI_NODE_PATH_ENV: str(node)}, agent_dir


class _FakeStdout:
    def __init__(self) -> None:
        self.records: deque[bytes] = deque()

    async def readline(self) -> bytes:
        return self.records.popleft() if self.records else b""


class _FakeStdin:
    def __init__(self, stdout: _FakeStdout) -> None:
        self.stdout = stdout

    def write(self, raw: bytes) -> None:
        request = json.loads(raw)
        request_id = request.get("id")
        if request.get("type") == "get_commands":
            records = [
                {
                    "type": "response",
                    "id": request_id,
                    "success": True,
                    "data": {"commands": [{"name": "inspect", "source": "extension"}]},
                }
            ]
        elif request.get("type") == "prompt":
            records = [
                {"type": "response", "id": request_id, "success": True},
                {"type": "tool_execution_start", "toolName": "inspect"},
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Local Pi completed the request.",
                                }
                            ],
                        }
                    ],
                },
            ]
        elif request.get("type") == "get_usage":
            records = [
                {
                    "type": "response",
                    "id": request_id,
                    "success": True,
                    "data": {
                        "tokens": {"input": 12, "output": 3, "total": 15},
                        "cost": 0.02,
                    },
                }
            ]
        else:
            records = []
        self.stdout.records.extend(
            (json.dumps(record) + "\n").encode() for record in records
        )

    async def drain(self) -> None:
        return None


class _FakeProcess:
    def __init__(self) -> None:
        self.stdout = _FakeStdout()
        self.stdin = _FakeStdin(self.stdout)
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = 0

    async def wait(self) -> int:
        self.returncode = 0
        return 0


async def _fake_process_factory(*_: Any, **__: Any) -> _FakeProcess:
    return _FakeProcess()


class _FakeProcessFactory:
    def __init__(self) -> None:
        self.processes: list[_FakeProcess] = []

    async def __call__(self, *_: Any, **__: Any) -> _FakeProcess:
        process = _FakeProcess()
        self.processes.append(process)
        return process


def test_config_resolution_uses_override_then_conventional_then_no_override(
    tmp_path: Path,
) -> None:
    override = tmp_path / "override"
    override.mkdir()
    assert resolve_pi_config({PI_CONFIG_PATH_ENV: str(override)}).source == "override"
    home = tmp_path / "home"
    (home / ".pi" / "agent").mkdir(parents=True)
    assert resolve_pi_config({}, home=home).source == "conventional"
    assert resolve_pi_config({}, home=tmp_path / "empty").source == "vanilla"


def test_vanilla_config_requires_an_explicit_cli_path(tmp_path: Path) -> None:
    environment, _ = _runtime_environment(tmp_path)
    resolution = resolve_pi_config({}, home=tmp_path / "empty")

    with pytest.raises(PiConfigError, match=PI_CLI_PATH_ENV):
        PiRuntime.from_environment(
            resolution, {PI_NODE_PATH_ENV: environment[PI_NODE_PATH_ENV]}
        )


def test_config_override_is_bounded_and_runtime_contract_is_exact(
    tmp_path: Path,
) -> None:
    with pytest.raises(PiConfigError, match="absolute"):
        resolve_pi_config({PI_CONFIG_PATH_ENV: "relative"})
    environment, _ = _runtime_environment(tmp_path)
    runtime = PiRuntime.from_environment(resolve_pi_config(environment), environment)
    assert runtime.command()[-3:] == ("--mode", "rpc", "--continue")


def test_realtime_schema_is_small_and_structured() -> None:
    inventory, prompt = realtime_functions()
    assert inventory.name == PI_TOOL_INVENTORY_NAME
    assert inventory.parameters["properties"] == {}
    assert inventory.as_dict()["type"] == "function"
    assert prompt.name == PI_TOOL_PROMPT_NAME
    assert prompt.parameters["required"] == ["message"]


@pytest.mark.asyncio
async def test_inventory_uses_declared_local_config_without_exposing_values(
    tmp_path: Path,
) -> None:
    environment, agent_dir = _runtime_environment(tmp_path)
    (agent_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"hindsight": {"token": "not-for-output"}}}),
        encoding="utf-8",
    )
    telemetry = []
    harness = PiRealtimeHarness(
        environment=environment,
        process_factory=_fake_process_factory,
        telemetry_sink=telemetry.append,
    )

    result = await harness.dispatch(PI_TOOL_INVENTORY_NAME, {})

    assert result == {
        "status": "ok",
        "inventory": {
            "mcp_servers": ["hindsight"],
            "commands": [{"name": "inspect", "source": "extension"}],
        },
        "session_reused": False,
    }
    assert harness.last_inventory == result["inventory"]
    assert telemetry[0].config_source == "override"
    assert telemetry[0].session_reused is False
    assert "not-for-output" not in json.dumps(asdict(telemetry[0]))


@pytest.mark.asyncio
async def test_prompt_forwards_to_pi_and_records_aggregate_usage_only(
    tmp_path: Path,
) -> None:
    environment, _ = _runtime_environment(tmp_path)
    telemetry = []
    harness = PiRealtimeHarness(
        environment=environment,
        process_factory=_fake_process_factory,
        telemetry_sink=telemetry.append,
    )

    result = await harness.dispatch(
        PI_TOOL_PROMPT_NAME, {"message": "inspect the selected session"}
    )

    assert result == {
        "status": "ok",
        "result": {"text": "Local Pi completed the request.", "tool_calls": 1},
        "session_reused": False,
    }
    assert asdict(telemetry[0]) == {
        "operation": PI_TOOL_PROMPT_NAME,
        "elapsed_ms": telemetry[0].elapsed_ms,
        "success": True,
        "config_source": "override",
        "command_count": 0,
        "tool_call_count": 1,
        "tokens": {"input": 12, "output": 3, "total": 15},
        "cost": 0.02,
        "session_reused": False,
        "error": None,
    }
    assert "inspect the selected session" not in json.dumps(asdict(telemetry[0]))


@pytest.mark.asyncio
async def test_warm_session_is_reused_and_closed(tmp_path: Path) -> None:
    environment, _ = _runtime_environment(tmp_path)
    telemetry = []
    factory = _FakeProcessFactory()
    harness = PiRealtimeHarness(
        environment=environment,
        process_factory=factory,
        telemetry_sink=telemetry.append,
    )

    await harness.dispatch(PI_TOOL_INVENTORY_NAME, {})
    result = await harness.dispatch(PI_TOOL_INVENTORY_NAME, {})
    await harness.aclose()

    assert result["session_reused"] is True
    assert len(factory.processes) == 1
    assert factory.processes[0].returncode == 0
    assert telemetry[1].session_reused is True


@pytest.mark.asyncio
async def test_realtime_adapter_returns_a_compact_function_output(
    tmp_path: Path,
) -> None:
    environment, _ = _runtime_environment(tmp_path)
    harness = PiRealtimeHarness(
        environment=environment,
        process_factory=_fake_process_factory,
        telemetry_sink=lambda _: None,
    )

    event = await harness.adapt_realtime_function_call(
        call_id="call-1", name=PI_TOOL_INVENTORY_NAME, arguments_json="{}"
    )

    assert event["type"] == "conversation.item.create"
    assert event["item"]["call_id"] == "call-1"
    assert json.loads(event["item"]["output"])["status"] == "ok"


@pytest.mark.asyncio
async def test_realtime_adapter_hides_local_runtime_errors(tmp_path: Path) -> None:
    environment, _ = _runtime_environment(tmp_path)
    harness = PiRealtimeHarness(
        environment={
            PI_CONFIG_PATH_ENV: "relative",
            PI_NODE_PATH_ENV: environment[PI_NODE_PATH_ENV],
        },
        telemetry_sink=lambda _: None,
    )

    event = await harness.adapt_realtime_function_call(
        call_id="call-1", name=PI_TOOL_INVENTORY_NAME, arguments_json="{}"
    )

    assert json.loads(event["item"]["output"]) == {
        "status": "error",
        "error": "PiConfigError",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name, arguments",
    [
        ("unknown", {}),
        (PI_TOOL_INVENTORY_NAME, {"x": 1}),
        (PI_TOOL_PROMPT_NAME, {}),
        (PI_TOOL_PROMPT_NAME, {"message": ""}),
        (PI_TOOL_PROMPT_NAME, {"message": "ok", "extra": True}),
    ],
)
async def test_dispatch_rejects_malformed_function_calls(
    name: object, arguments: object
) -> None:
    with pytest.raises(PiHarnessError):
        await PiRealtimeHarness(telemetry_sink=lambda _: None).dispatch(name, arguments)
