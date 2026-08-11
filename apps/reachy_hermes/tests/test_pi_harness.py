from __future__ import annotations

import ast
import inspect
import json

import pytest

from reachy_hermes import pi_harness
from reachy_hermes.pi_harness import (
    HARNESS_TOOL_NAME,
    PiHarnessRequestError,
    PiRealtimeHarnessConfig,
    create_pi_realtime_harness,
)


def _harness():
    return create_pi_realtime_harness(PiRealtimeHarnessConfig(integration_name="realtime-test"))


def test_harness_exposes_exactly_one_empty_schema_allowlisted_tool() -> None:
    tool = _harness().tools

    assert len(tool) == 1
    assert tool[0].name == HARNESS_TOOL_NAME
    assert tool[0].parameters.properties == {}
    assert tool[0].parameters.required == []


@pytest.mark.asyncio
async def test_capabilities_tool_is_local_read_only_and_deterministic() -> None:
    result = await _harness().tools[0].execute("tool-call-1", {})

    assert len(result.content) == 1
    assert json.loads(result.content[0].text) == {
        "actuation": False,
        "capabilities": ["deterministic", "read_only"],
        "external_access": False,
        "integration_name": "realtime-test",
        "tools": [HARNESS_TOOL_NAME],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("unknown", {}),
        (HARNESS_TOOL_NAME, None),
        (HARNESS_TOOL_NAME, []),
        (HARNESS_TOOL_NAME, {"unexpected": True}),
    ],
)
async def test_dispatch_rejects_unknown_or_malformed_calls(name: object, arguments: object) -> None:
    with pytest.raises(PiHarnessRequestError):
        await _harness().dispatch(name, arguments)


@pytest.mark.asyncio
async def test_tool_rejects_a_missing_call_identifier() -> None:
    with pytest.raises(PiHarnessRequestError, match="tool_call_id"):
        await _harness().tools[0].execute("", {})


def test_factory_requires_the_explicit_config_type() -> None:
    with pytest.raises(TypeError, match="PiRealtimeHarnessConfig"):
        create_pi_realtime_harness(object())  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["", "has spaces", "/tmp", "https://example.test", None])
def test_config_rejects_non_identifier_integration_names(value: object) -> None:
    with pytest.raises(ValueError, match="integration_name"):
        PiRealtimeHarnessConfig(integration_name=value)  # type: ignore[arg-type]


def test_adapter_source_has_no_discovery_or_external_access_imports() -> None:
    source = inspect.getsource(pi_harness)
    module = ast.parse(source)
    imported_modules = {
        alias.name.split(".")[0]
        for statement in ast.walk(module)
        if isinstance(statement, ast.Import)
        for alias in statement.names
    } | {
        statement.module.split(".")[0]
        for statement in ast.walk(module)
        if isinstance(statement, ast.ImportFrom) and statement.module
    }

    assert imported_modules.isdisjoint(
        {"asyncio", "http", "os", "pathlib", "socket", "subprocess", "urllib"}
    )
    assert all(
        forbidden not in source
        for forbidden in ("getenv", "expanduser", "getcwd", "create_subprocess", "ReachyMini")
    )
