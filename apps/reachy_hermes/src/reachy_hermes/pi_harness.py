"""Local-host Realtime bridge for an operator-managed Pi session.

Pi remains external to this Python workspace.  ``PI_CONFIG_PATH`` is a bounded
override for Pi's documented ``PI_CODING_AGENT_DIR``; otherwise only the
documented ``~/.pi/agent`` location is considered. Without either directory,
Pi receives no configuration override, but this bridge does not discover a
vanilla Pi CLI: ``PI_CLI_PATH`` is required. No user-home scan occurs.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

PI_CONFIG_PATH_ENV: Final = "PI_CONFIG_PATH"
PI_NODE_PATH_ENV: Final = "PI_NODE_PATH"
PI_CLI_PATH_ENV: Final = "PI_CLI_PATH"
PI_TELEMETRY_PATH_ENV: Final = "PI_TELEMETRY_PATH"
PI_RUNTIME_PACKAGE: Final = "@mariozechner/pi-coding-agent"
PI_RUNTIME_VERSION: Final = "0.73.1"
PI_TOOL_INVENTORY_NAME: Final = "get_pi_tool_inventory"
PI_TOOL_PROMPT_NAME: Final = "invoke_pi_session"
HARNESS_TOOL_NAME: Final = PI_TOOL_INVENTORY_NAME
_MAX_RESULT_CHARS: Final = 1_500


class PiHarnessError(RuntimeError):
    """A concise failure safe to present to a Realtime caller."""


class PiConfigError(PiHarnessError):
    """The selected external Pi configuration/runtime cannot be used."""


class PiRpcError(PiHarnessError):
    """Pi did not complete a documented JSONL RPC request."""


@dataclass(frozen=True, slots=True)
class PiConfigResolution:
    agent_dir: Path | None
    source: str

    @property
    def environment(self) -> dict[str, str]:
        return (
            {}
            if self.agent_dir is None
            else {"PI_CODING_AGENT_DIR": str(self.agent_dir)}
        )


def resolve_pi_config(
    environment: Mapping[str, str] | None = None, *, home: Path | None = None
) -> PiConfigResolution:
    """Resolve one configured Pi agent directory, or no config override."""
    values = os.environ if environment is None else environment
    override = values.get(PI_CONFIG_PATH_ENV)
    if override is not None:
        path = Path(override).expanduser()
        if not override.strip() or not path.is_absolute() or not path.is_dir():
            raise PiConfigError(
                f"{PI_CONFIG_PATH_ENV} must be an existing absolute Pi agent directory"
            )
        return PiConfigResolution(path.resolve(), "override")
    conventional = (Path.home() if home is None else home) / ".pi" / "agent"
    if conventional.is_dir():
        return PiConfigResolution(conventional.resolve(), "conventional")
    return PiConfigResolution(None, "vanilla")


@dataclass(frozen=True, slots=True)
class PiRuntime:
    node: Path
    cli: Path

    @classmethod
    def from_environment(
        cls,
        resolution: PiConfigResolution,
        environment: Mapping[str, str] | None = None,
    ) -> "PiRuntime":
        values = os.environ if environment is None else environment
        cli_override = values.get(PI_CLI_PATH_ENV)
        cli = (
            Path(cli_override).expanduser()
            if cli_override
            else (
                resolution.agent_dir
                / "npm"
                / "node_modules"
                / PI_RUNTIME_PACKAGE
                / "dist"
                / "cli.js"
                if resolution.agent_dir is not None
                else Path()
            )
        )
        node_override = values.get(PI_NODE_PATH_ENV)
        node = (
            Path(node_override).expanduser()
            if node_override
            else Path(shutil.which("node") or "")
        )
        if not cli.is_file():
            raise PiConfigError(
                "Pi CLI entrypoint is unavailable; set PI_CLI_PATH when no selected Pi agent directory contains it"
            )
        if not node.is_file():
            raise PiConfigError(
                "Node is unavailable; set PI_NODE_PATH for this local host"
            )
        runtime = cls(node.resolve(), cli.resolve())
        runtime.verify_contract()
        return runtime

    def verify_contract(self) -> None:
        try:
            manifest = json.loads(
                (self.cli.parent.parent / "package.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise PiConfigError("Pi CLI package manifest cannot be read") from exc
        if (
            manifest.get("name") != PI_RUNTIME_PACKAGE
            or manifest.get("version") != PI_RUNTIME_VERSION
        ):
            raise PiConfigError(
                "Pi CLI package does not match the supported local Pi runtime contract"
            )

    def command(self) -> tuple[str, ...]:
        # --continue attaches Pi's persisted session for this local host's service cwd.
        return (str(self.node), str(self.cli), "--mode", "rpc", "--continue")


@dataclass(frozen=True, slots=True)
class RealtimeFunction:
    name: str
    description: str
    parameters: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"type": "function", **asdict(self)}


def realtime_functions() -> tuple[RealtimeFunction, ...]:
    return (
        RealtimeFunction(
            PI_TOOL_INVENTORY_NAME,
            "List Pi commands and configured MCP servers available to the local Pi session.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        RealtimeFunction(
            PI_TOOL_PROMPT_NAME,
            "Forward one bounded request to the persisted local Pi session.",
            {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "minLength": 1, "maxLength": 4000}
                },
                "required": ["message"],
                "additionalProperties": False,
            },
        ),
    )


@dataclass(frozen=True, slots=True)
class PiTelemetry:
    operation: str
    elapsed_ms: int
    success: bool
    config_source: str
    command_count: int
    tool_call_count: int
    tokens: dict[str, int]
    cost: float | None
    session_reused: bool
    error: str | None


class JsonlTelemetrySink:
    """Append only aggregate metrics: never prompts, paths, output, or config values."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __call__(self, telemetry: PiTelemetry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(telemetry), sort_keys=True) + "\n")


def default_telemetry_sink(
    environment: Mapping[str, str] | None = None, *, home: Path | None = None
) -> JsonlTelemetrySink:
    values = os.environ if environment is None else environment
    configured = values.get(PI_TELEMETRY_PATH_ENV)
    path = (
        Path(configured).expanduser()
        if configured
        else (Path.home() if home is None else home)
        / ".local/state/reachy-hermes/pi-realtime.jsonl"
    )
    return JsonlTelemetrySink(path)


def _mcp_server_names(agent_dir: Path | None) -> list[str]:
    if agent_dir is None:
        return []
    try:
        payload = json.loads((agent_dir / "mcp.json").read_text(encoding="utf-8"))
        servers = payload.get("mcpServers", {}) if isinstance(payload, dict) else {}
        return sorted(name for name in servers if isinstance(name, str))
    except (OSError, json.JSONDecodeError):
        return []


def _assistant_text(messages: object) -> str:
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content.replace("\x00", "").strip()[:_MAX_RESULT_CHARS]
        if isinstance(content, list):
            text = "".join(
                item.get("text", "") for item in content if isinstance(item, dict)
            )
            return text.replace("\x00", "").strip()[:_MAX_RESULT_CHARS]
    return ""


ProcessFactory = Callable[..., Awaitable[Any]]


class PiRpcSession:
    """A warm Pi JSONL RPC client using its real local config and session."""

    def __init__(
        self,
        runtime: PiRuntime,
        resolution: PiConfigResolution,
        *,
        process_factory: ProcessFactory | None = None,
    ) -> None:
        self.runtime = runtime
        self.resolution = resolution
        self.process_factory = process_factory or asyncio.create_subprocess_exec
        self.process: Any | None = None

    async def start(self) -> "PiRpcSession":
        if self.process is not None:
            return self
        env = os.environ.copy()
        env.update(self.resolution.environment)
        try:
            self.process = await self.process_factory(
                *self.runtime.command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
        except OSError as exc:
            raise PiRpcError("Pi RPC process could not be started") from exc
        return self

    async def __aenter__(self) -> "PiRpcSession":
        return await self.start()

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        process, self.process = self.process, None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                pass

    async def command(
        self, request: dict[str, Any], *, turn: bool = False
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if (
            self.process is None
            or self.process.stdin is None
            or self.process.stdout is None
        ):
            raise PiRpcError("Pi RPC session is not running")
        if self.process.returncode is not None:
            raise PiRpcError("Pi RPC session stopped unexpectedly")
        request_id = f"reachy-{time.monotonic_ns()}"
        request["id"] = request_id
        try:
            self.process.stdin.write(
                (json.dumps(request, separators=(",", ":")) + "\n").encode()
            )
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionError) as exc:
            raise PiRpcError("Pi RPC session closed while sending a request") from exc
        response: dict[str, Any] | None = None
        events: list[dict[str, Any]] = []
        while response is None or (
            turn and not any(event.get("type") == "agent_end" for event in events)
        ):
            try:
                raw = await asyncio.wait_for(self.process.stdout.readline(), 120)
            except TimeoutError as exc:
                raise PiRpcError(
                    "Pi RPC did not complete before the local timeout"
                ) from exc
            if not raw:
                raise PiRpcError("Pi RPC closed before completing the request")
            try:
                item = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(item, dict):
                continue
            if item.get("type") == "extension_ui_request":
                # Realtime calls cannot silently approve external actions.
                try:
                    self.process.stdin.write(
                        (
                            json.dumps(
                                {
                                    "type": "extension_ui_response",
                                    "id": item.get("id"),
                                    "cancelled": True,
                                }
                            )
                            + "\n"
                        ).encode()
                    )
                    await self.process.stdin.drain()
                except (BrokenPipeError, ConnectionError) as exc:
                    raise PiRpcError(
                        "Pi RPC session closed while rejecting an extension dialog"
                    ) from exc
            elif item.get("type") == "response" and item.get("id") == request_id:
                response = item
            else:
                events.append(item)
        if not response.get("success"):
            raise PiRpcError("Pi rejected the RPC request")
        return response, events


class PiRealtimeHarness:
    """Expose Pi inventory/prompt calls as provider-neutral Realtime functions."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        process_factory: ProcessFactory | None = None,
        telemetry_sink: Callable[[PiTelemetry], None] | None = None,
    ) -> None:
        self.environment = dict(os.environ if environment is None else environment)
        self.process_factory = process_factory
        self.telemetry_sink = telemetry_sink or default_telemetry_sink(self.environment)
        self._session: PiRpcSession | None = None
        self._session_key: tuple[PiRuntime, PiConfigResolution] | None = None
        self._lock = asyncio.Lock()
        self._last_inventory: dict[str, Any] | None = None

    @property
    def tools(self) -> tuple[RealtimeFunction, ...]:
        return realtime_functions()

    @property
    def last_inventory(self) -> dict[str, Any] | None:
        """Return the latest secret-safe inventory observed from the local Pi session."""
        return (
            None
            if self._last_inventory is None
            else json.loads(json.dumps(self._last_inventory))
        )

    async def __aenter__(self) -> "PiRealtimeHarness":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Terminate the warm local Pi process during application shutdown."""
        async with self._lock:
            if self._session is not None:
                await self._session.aclose()
            self._session = None
            self._session_key = None

    async def dispatch(self, name: object, arguments: object) -> dict[str, Any]:
        if name not in {PI_TOOL_INVENTORY_NAME, PI_TOOL_PROMPT_NAME} or not isinstance(
            arguments, dict
        ):
            raise PiHarnessError("unknown or malformed Pi Realtime function")
        if name == PI_TOOL_INVENTORY_NAME and arguments:
            raise PiHarnessError("Pi tool inventory accepts no arguments")
        if name == PI_TOOL_PROMPT_NAME:
            message = arguments.get("message")
            if (
                set(arguments) != {"message"}
                or not isinstance(message, str)
                or not message.strip()
                or len(message) > 4000
            ):
                raise PiHarnessError(
                    "Pi session prompt must contain one bounded message"
                )
        return await self._invoke(str(name), arguments)

    async def adapt_realtime_function_call(
        self, *, call_id: object, name: object, arguments_json: object
    ) -> dict[str, Any]:
        """Translate one Realtime function call into its function-call-output event.

        The caller sends the returned event to its Realtime connection, then
        requests the next response. Pi configuration/runtime errors become a
        compact error payload so a broken local tool cannot terminate the voice
        session.
        """
        if not isinstance(call_id, str) or not call_id:
            raise PiHarnessError("Realtime function call requires a non-empty call_id")
        try:
            arguments = (
                json.loads(arguments_json)
                if isinstance(arguments_json, str)
                else arguments_json
            )
        except json.JSONDecodeError:
            arguments = None
        try:
            output = await self.dispatch(name, arguments)
        except PiHarnessError as exc:
            output = {"status": "error", "error": type(exc).__name__}
        return {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(output, separators=(",", ":")),
            },
        }

    async def _ensure_session(
        self, runtime: PiRuntime, resolution: PiConfigResolution
    ) -> tuple[PiRpcSession, bool]:
        key = (runtime, resolution)
        if (
            self._session is not None
            and self._session_key == key
            and self._session.process is not None
            and self._session.process.returncode is None
        ):
            return self._session, True
        if self._session is not None:
            await self._session.aclose()
        self._session = await PiRpcSession(
            runtime, resolution, process_factory=self.process_factory
        ).start()
        self._session_key = key
        return self._session, False

    async def _invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        command_count = tool_calls = 0
        tokens: dict[str, int] = {}
        cost: float | None = None
        success = False
        session_reused = False
        error: str | None = None
        config_source = "unresolved"
        active_session: PiRpcSession | None = None
        try:
            async with self._lock:
                resolution = resolve_pi_config(self.environment)
                config_source = resolution.source
                runtime = PiRuntime.from_environment(resolution, self.environment)
                session, session_reused = await self._ensure_session(
                    runtime, resolution
                )
                active_session = session
                if name == PI_TOOL_INVENTORY_NAME:
                    response, _ = await session.command({"type": "get_commands"})
                    commands = (
                        response.get("data", {}).get("commands", [])
                        if isinstance(response.get("data"), dict)
                        else []
                    )
                    command_count = len(commands) if isinstance(commands, list) else 0
                    inventory = {
                        "mcp_servers": _mcp_server_names(resolution.agent_dir),
                        "commands": [
                            {"name": item.get("name"), "source": item.get("source")}
                            for item in commands
                            if isinstance(item, dict)
                            and isinstance(item.get("name"), str)
                        ],
                    }
                    self._last_inventory = inventory
                    result = {
                        "status": "ok",
                        "inventory": inventory,
                        "session_reused": session_reused,
                    }
                else:
                    _, events = await session.command(
                        {"type": "prompt", "message": arguments["message"]}, turn=True
                    )
                    tool_calls = sum(
                        event.get("type") == "tool_execution_start" for event in events
                    )
                    usage, _ = await session.command({"type": "get_usage"})
                    data = (
                        usage.get("data", {})
                        if isinstance(usage.get("data"), dict)
                        else {}
                    )
                    tokens = {
                        key: value
                        for key, value in data.get("tokens", {}).items()
                        if isinstance(key, str) and isinstance(value, int)
                    }
                    raw_cost = data.get("cost")
                    cost = (
                        float(raw_cost) if isinstance(raw_cost, (int, float)) else None
                    )
                    result = {
                        "status": "ok",
                        "result": {
                            "text": _assistant_text(
                                next(
                                    (
                                        event.get("messages")
                                        for event in reversed(events)
                                        if event.get("type") == "agent_end"
                                    ),
                                    [],
                                )
                            ),
                            "tool_calls": tool_calls,
                        },
                        "session_reused": session_reused,
                    }
                success = True
                return result
        except PiRpcError as exc:
            error = type(exc).__name__
            async with self._lock:
                if self._session is active_session:
                    await self._session.aclose()
                    self._session = None
                    self._session_key = None
            raise
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            self.telemetry_sink(
                PiTelemetry(
                    name,
                    round((time.perf_counter() - started) * 1000),
                    success,
                    config_source,
                    command_count,
                    tool_calls,
                    tokens,
                    cost,
                    session_reused,
                    error,
                )
            )


def create_pi_realtime_harness(**kwargs: Any) -> PiRealtimeHarness:
    return PiRealtimeHarness(**kwargs)


__all__ = [
    "HARNESS_TOOL_NAME",
    "PI_CONFIG_PATH_ENV",
    "PI_RUNTIME_PACKAGE",
    "PI_RUNTIME_VERSION",
    "PI_TOOL_INVENTORY_NAME",
    "PI_TOOL_PROMPT_NAME",
    "JsonlTelemetrySink",
    "PiConfigError",
    "PiConfigResolution",
    "PiHarnessError",
    "PiRealtimeHarness",
    "PiRpcError",
    "PiRuntime",
    "PiTelemetry",
    "RealtimeFunction",
    "create_pi_realtime_harness",
    "default_telemetry_sink",
    "realtime_functions",
    "resolve_pi_config",
]
