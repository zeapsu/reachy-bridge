"""Bounded Pi tool adapter for a future Realtime integration.

This module deliberately does not create an agent, select a provider, or load
configuration. Its sole external configuration entry point is
``create_pi_realtime_harness(config)``. The resulting harness can expose and
execute only the local, deterministic tools declared below.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final

from pi_agent_core import AgentTool, AgentToolResult, AgentToolSchema, TextContent


HARNESS_TOOL_NAME: Final = "get_realtime_tool_capabilities"
"""The only Pi tool this adapter exposes."""


@dataclass(frozen=True, slots=True)
class PiRealtimeHarnessConfig:
    """Explicit, non-secret configuration for the bounded harness.

    ``integration_name`` is returned by the deterministic capability tool so a
    caller can identify the configured integration. It is not interpreted as a
    URL, path, command, provider, or credential.
    """

    integration_name: str

    def __post_init__(self) -> None:
        if not isinstance(self.integration_name, str) or not re.fullmatch(
            r"[a-z][a-z0-9_-]{0,63}", self.integration_name
        ):
            raise ValueError("integration_name must be a safe identifier")


class PiHarnessRequestError(ValueError):
    """Raised when a Pi tool request is not part of the explicit allowlist."""


class PiRealtimeHarness:
    """Expose and dispatch the one read-only deterministic Pi tool."""

    def __init__(self, config: PiRealtimeHarnessConfig) -> None:
        self._config = config
        self._tools = (self._build_capabilities_tool(),)

    @property
    def tools(self) -> tuple[AgentTool, ...]:
        """Return the complete, fixed allowlist of Pi tool definitions."""
        return self._tools

    async def dispatch(self, name: object, arguments: object) -> AgentToolResult:
        """Validate and execute one allowlisted tool call, failing closed."""
        if name != HARNESS_TOOL_NAME:
            raise PiHarnessRequestError("unknown Pi harness tool")
        if not isinstance(arguments, dict):
            raise PiHarnessRequestError("tool arguments must be an object")
        if arguments:
            raise PiHarnessRequestError("tool does not accept arguments")
        return self._capabilities_result()

    def _build_capabilities_tool(self) -> AgentTool:
        async def execute(tool_call_id: str, params: dict[str, Any], **_: Any) -> AgentToolResult:
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise PiHarnessRequestError("tool_call_id must be a non-empty string")
            return await self.dispatch(HARNESS_TOOL_NAME, params)

        return AgentTool(
            name=HARNESS_TOOL_NAME,
            label="Realtime tool capabilities",
            description="Return the fixed safety capabilities of this Realtime tool harness.",
            parameters=AgentToolSchema(properties={}, required=[]),
            execute=execute,
        )

    def _capabilities_result(self) -> AgentToolResult:
        payload = {
            "integration_name": self._config.integration_name,
            "tools": [HARNESS_TOOL_NAME],
            "capabilities": ["deterministic", "read_only"],
            "actuation": False,
            "external_access": False,
        }
        return AgentToolResult(content=[TextContent(text=json.dumps(payload, sort_keys=True))])


def create_pi_realtime_harness(config: PiRealtimeHarnessConfig) -> PiRealtimeHarness:
    """Create a harness from the only supported explicit configuration input."""
    if not isinstance(config, PiRealtimeHarnessConfig):
        raise TypeError("config must be PiRealtimeHarnessConfig")
    return PiRealtimeHarness(config)


__all__ = [
    "HARNESS_TOOL_NAME",
    "PiHarnessRequestError",
    "PiRealtimeHarness",
    "PiRealtimeHarnessConfig",
    "create_pi_realtime_harness",
]
