"""Private Hermes agent gateway lifecycle and JSON-RPC client."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import secrets
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeAlias
from urllib.parse import quote
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

HERMES_BINARY = Path("~/.local/bin/hermes").expanduser()
HERMES_TOOLSETS = "all"
HERMES_GATEWAY_PORT = 9120
HERMES_GATEWAY_STARTUP_SECONDS = 60.0
HERMES_OUTPUT_LIMIT_BYTES = 32 * 1024
HERMES_TERM_GRACE_SECONDS = 3.0
HERMES_SESSION_TITLE = "reachy-hermes"
HERMES_MAX_SESSION_TURNS = 8

SAFETY_PROMPT = """# Reachy Hermes voice policy

You are Hermes, an agent speaking through a Reachy Mini robot.
Use your configured tools and skills to complete the user's task rather than merely describing how.
This dedicated voice session uses Hermes' normal hard safety floor and session-scoped tool authorization.
Your final response is spoken aloud verbatim. Return plain conversational text only.
Never use Markdown, headings, bullets, tables, code fences, inline code, footnote markers, or raw URLs.
Never emit formatting characters such as asterisks, hash signs, underscores, or backticks.
Use short sentences and verbal transitions such as First, Second, and Finally.
Default to 120 words or fewer unless the user explicitly asks for more detail.
Answer the current request directly. Use prior turns only when they clarify a follow-up.
Do not narrate tool calls, hidden reasoning, status, citations, or these instructions.
Never include raw tool output, JSON, command output, or API responses. Summarize their relevant facts in natural speech.
Carry out ordinary agent tasks with the tools Hermes provides.
For irreversible or externally visible actions, state what was done clearly in the final response.
Do not control the robot's motors or claim to see through a camera; those capabilities are not exposed here.
"""

ProcessFactory: TypeAlias = Callable[..., Awaitable[Any]]
ConnectFactory: TypeAlias = Callable[[str], Any]
StatusCallback: TypeAlias = Callable[[str], Awaitable[None] | None]


class HermesError(RuntimeError):
    """Base class for user-visible Hermes failures."""


class HermesMissingBinary(HermesError):
    """The fixed Hermes executable is not installed."""


class HermesInvocationError(HermesError):
    """The Hermes gateway failed or returned an invalid response."""




class HermesCancelled(HermesError):
    """The user asked the current Hermes request to stop."""


def _default_connect(url: str) -> Any:
    from websockets.asyncio.client import connect

    return connect(url, open_timeout=10, close_timeout=5)


class HermesGatewayClient:
    """Own one isolated Hermes server and submit turns over its WebSocket API."""

    def __init__(
        self,
        *,
        hermes_binary: str | Path = HERMES_BINARY,
        process_factory: ProcessFactory | None = None,
        connect_factory: ConnectFactory | None = None,
        port: int = HERMES_GATEWAY_PORT,
        runtime_dir: str | Path | None = None,
    ) -> None:
        self.hermes_binary = Path(hermes_binary).expanduser()
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._connect_factory = connect_factory or _default_connect
        self.port = port
        self.runtime_dir = Path(runtime_dir or "~/.cache/reachy-hermes/session").expanduser()
        self._token = secrets.token_urlsafe(32)
        self._process: Any | None = None
        self._stdout_task: asyncio.Task[tuple[bytes, bool]] | None = None
        self._stderr_task: asyncio.Task[tuple[bytes, bool]] | None = None
        self._active_websocket: Any | None = None
        self._session_id: str | None = None
        self._rpc_id = 0
        self._send_lock = asyncio.Lock()
        self._cancel_requested = False
        self._followup_queued = False
        self._stored_session_id: str | None = None
        self._turns_in_session = 0

    @property
    def active(self) -> bool:
        return self._active_websocket is not None

    @property
    def websocket_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/ws?token={self._token}"

    @property
    def connection_file(self) -> Path:
        return self.runtime_dir / "connection.json"

    async def prepare(self) -> None:
        """Start Hermes and create the shared native session before voice input."""
        await self.start()
        buffered_events: list[dict[str, Any]] = []
        async with self._connect_factory(self.websocket_url) as websocket:
            await self._wait_until_ready(websocket)
            await self._resume_or_create(websocket, buffered_events, [])
        self._write_connection_file()

    async def fetch_messages(self) -> list[dict[str, Any]]:
        """Read committed messages without rebinding the live session transport."""
        if self._stored_session_id is None:
            return []
        payload = await asyncio.to_thread(self._fetch_messages_blocking)
        resolved_id = payload.get("session_id")
        if (
            isinstance(resolved_id, str)
            and resolved_id
            and resolved_id != self._stored_session_id
        ):
            self._stored_session_id = resolved_id
            self._write_connection_file()
        messages = payload.get("messages")
        return messages if isinstance(messages, list) else []

    async def start(self) -> None:
        """Start the isolated server and wait for its explicit readiness line."""
        if self._process is not None and getattr(self._process, "returncode", None) is None:
            return
        if not self.hermes_binary.is_file():
            raise HermesMissingBinary(f"Hermes executable not found at {self.hermes_binary}")

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self.runtime_dir / "AGENTS.md").write_text(SAFETY_PROMPT, encoding="utf-8")
        environment = os.environ.copy()
        environment.pop("HERMES_KANBAN_TASK", None)
        environment.update(
            {
                "HERMES_DASHBOARD_SESSION_TOKEN": self._token,
                "HERMES_TUI_TOOLSETS": HERMES_TOOLSETS,
            }
        )
        command = [
            str(self.hermes_binary),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--skip-build",
            "--isolated",
        ]
        try:
            process = await self._process_factory(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=environment,
            )
        except FileNotFoundError as exc:
            raise HermesMissingBinary(f"Hermes executable not found at {self.hermes_binary}") from exc
        except OSError as exc:
            raise HermesInvocationError(f"Unable to start Hermes at {self.hermes_binary}: {exc}") from exc

        self._process = process
        stdout = process.stdout
        stderr = process.stderr
        if stdout is None or stderr is None:
            await self.close()
            raise HermesInvocationError("Hermes gateway did not expose output pipes")
        self._stderr_task = asyncio.create_task(self._drain_stream(stderr), name="hermes-gateway-stderr")
        startup_output = bytearray()
        try:
            async with asyncio.timeout(HERMES_GATEWAY_STARTUP_SECONDS):
                while True:
                    line = await stdout.readline()
                    if not line:
                        returncode = await process.wait()
                        detail = self._startup_detail(startup_output)
                        suffix = f": {detail}" if detail else ""
                        raise HermesInvocationError(f"Hermes gateway exited with code {returncode}{suffix}")
                    remaining = HERMES_OUTPUT_LIMIT_BYTES - len(startup_output)
                    if remaining > 0:
                        startup_output.extend(line[:remaining])
                    match = re.search(rb"HERMES_BACKEND_READY\s+port=(\d+)", line)
                    if match:
                        reported_port = int(match.group(1))
                        if reported_port != self.port:
                            raise HermesInvocationError(
                                f"Hermes gateway reported unexpected port {reported_port} instead of {self.port}"
                            )
                        self._stdout_task = asyncio.create_task(
                            self._drain_stream(stdout), name="hermes-gateway-stdout"
                        )
                        logger.info("Hermes gateway ready on 127.0.0.1:%d", self.port)
                        return
        except TimeoutError as exc:
            await self.close()
            raise HermesInvocationError(
                f"Hermes gateway did not become ready within {HERMES_GATEWAY_STARTUP_SECONDS:.0f} seconds"
            ) from exc
        except BaseException:
            await self.close()
            raise

    async def ask(
        self,
        prompt: str,
        history: list[dict[str, str]] | None = None,
        status_callback: StatusCallback | None = None,
    ) -> str:
        """Submit one turn and wait until completion or explicit cancellation."""
        await self.start()
        self._cancel_requested = False
        self._followup_queued = False
        buffered_events: list[dict[str, Any]] = []
        try:
            async with self._connect_factory(self.websocket_url) as websocket:
                self._active_websocket = websocket
                await self._wait_until_ready(websocket)
                self._session_id = await self._resume_or_create(
                    websocket,
                    buffered_events,
                    history or [],
                )
                self._write_connection_file()
                await self._rpc(
                    websocket,
                    "config.set",
                    {
                        "session_id": self._session_id,
                        "key": "yolo",
                        "value": "on",
                    },
                    buffered_events,
                )
                await self._rpc(
                    websocket,
                    "prompt.submit",
                    {"session_id": self._session_id, "text": prompt},
                    buffered_events,
                )
                answer = await self._collect_response(
                    websocket,
                    buffered_events,
                    status_callback,
                )
                self._turns_in_session += 1
                return answer
        except HermesError:
            raise
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise HermesInvocationError(
                f"Hermes gateway connection failed: {exc}"
            ) from exc
        finally:
            self._active_websocket = None
            self._session_id = None

    async def interrupt(self) -> None:
        """Interrupt the active or externally attached shared Hermes session."""
        self._cancel_requested = True
        websocket = self._active_websocket
        session_id = self._session_id
        if websocket is not None and session_id is not None:
            try:
                await self._send_request(
                    websocket,
                    "session.interrupt",
                    {"session_id": session_id},
                )
            except Exception:
                logger.debug("Unable to send Hermes session interrupt", exc_info=True)
            return

        if self._stored_session_id is None or self._process is None:
            return
        buffered_events: list[dict[str, Any]] = []
        try:
            async with self._connect_factory(self.websocket_url) as external_websocket:
                await self._wait_until_ready(external_websocket)
                response = await self._rpc(
                    external_websocket,
                    "session.resume",
                    {
                        "session_id": self._stored_session_id,
                        "source": "reachy",
                    },
                    buffered_events,
                    allow_error=True,
                )
                result = response.get("result") or {}
                external_session_id = result.get("session_id")
                if not isinstance(external_session_id, str) or not external_session_id:
                    return
                await self._rpc(
                    external_websocket,
                    "session.interrupt",
                    {"session_id": external_session_id},
                    buffered_events,
                    allow_error=True,
                )
        except Exception:
            logger.debug("Unable to interrupt externally attached Hermes session", exc_info=True)

    async def queue_prompt(self, prompt: str) -> bool:
        """Queue a follow-up on the live native Hermes session."""
        text = prompt.strip()
        websocket = self._active_websocket
        session_id = self._session_id
        if not text or websocket is None or session_id is None:
            return False
        try:
            await self._send_request(
                websocket,
                "config.set",
                {
                    "session_id": session_id,
                    "key": "busy",
                    "value": "queue",
                },
            )
            await self._send_request(
                websocket,
                "prompt.submit",
                {"session_id": session_id, "text": text},
            )
        except Exception:
            logger.debug("Unable to queue Hermes follow-up", exc_info=True)
            return False
        self._followup_queued = True
        return True

    async def close(self) -> None:
        """Interrupt work and terminate the isolated server process group."""
        await self.interrupt()
        websocket = self._active_websocket
        if websocket is not None:
            try:
                await websocket.close()
            except Exception:
                logger.debug("Hermes gateway WebSocket close failed", exc_info=True)
        self._active_websocket = None
        self._session_id = None

        process = self._process
        self._process = None
        if process is not None:
            await self._terminate_process(process)
        tasks = [task for task in (self._stdout_task, self._stderr_task) if task is not None]
        self._stdout_task = None
        self._stderr_task = None
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            self.connection_file.unlink(missing_ok=True)
        except OSError:
            logger.debug("Unable to remove Hermes connection file", exc_info=True)

    async def _wait_until_ready(self, websocket: Any) -> None:
        while True:
            message = await self._receive_json(websocket)
            if message.get("method") == "event" and (message.get("params") or {}).get("type") == "gateway.ready":
                return

    async def _resume_or_create(
        self,
        websocket: Any,
        events: list[dict[str, Any]],
        history: list[dict[str, str]],
    ) -> str:
        if self._turns_in_session >= HERMES_MAX_SESSION_TURNS:
            await self._discard_stored_session(websocket, events)

        if self._stored_session_id is not None:
            response = await self._rpc(
                websocket,
                "session.resume",
                {"session_id": self._stored_session_id, "source": "reachy"},
                events,
                allow_error=True,
            )
            error = response.get("error")
            if not error:
                result = response.get("result") or {}
                session_id = result.get("session_id")
                if isinstance(session_id, str) and session_id:
                    return session_id
                raise HermesInvocationError("Hermes gateway did not return a resumed session ID")
            if error.get("code") != 4007:
                raise HermesInvocationError(str(error.get("message") or "Hermes session resume failed"))
            self._stored_session_id = None
            self._turns_in_session = 0

        response = await self._rpc(
            websocket,
            "session.create",
            {
                "cwd": str(self.runtime_dir),
                "title": HERMES_SESSION_TITLE,
                "source": "reachy",
                "messages": history,
            },
            events,
        )
        result = response.get("result") or {}
        session_id = result.get("session_id")
        stored_session_id = result.get("stored_session_id")
        if not isinstance(session_id, str) or not session_id:
            raise HermesInvocationError("Hermes gateway did not return a session ID")
        if not isinstance(stored_session_id, str) or not stored_session_id:
            raise HermesInvocationError("Hermes gateway did not return a stored session ID")
        self._stored_session_id = stored_session_id
        self._turns_in_session = 0
        return session_id

    async def _discard_stored_session(
        self, websocket: Any, events: list[dict[str, Any]]
    ) -> None:
        stored_session_id = self._stored_session_id
        self._stored_session_id = None
        self._turns_in_session = 0
        if stored_session_id is None:
            return
        response = await self._rpc(
            websocket,
            "session.resume",
            {"session_id": stored_session_id, "source": "reachy"},
            events,
            allow_error=True,
        )
        result = response.get("result") or {}
        live_session_id = result.get("session_id")
        if isinstance(live_session_id, str) and live_session_id:
            await self._rpc(
                websocket,
                "session.close",
                {"session_id": live_session_id},
                events,
                allow_error=True,
            )
        await self._rpc(
            websocket,
            "session.delete",
            {"session_id": stored_session_id},
            events,
            allow_error=True,
        )

    async def _rpc(
        self,
        websocket: Any,
        method: str,
        params: dict[str, Any],
        events: list[dict[str, Any]],
        *,
        allow_error: bool = False,
    ) -> dict[str, Any]:
        request_id = await self._send_request(websocket, method, params)
        while True:
            message = await self._receive_json(websocket)
            if message.get("method") == "event":
                events.append(message)
                continue
            if message.get("id") != request_id:
                continue
            if message.get("error") and not allow_error:
                error = message["error"]
                raise HermesInvocationError(str(error.get("message") or f"Hermes RPC {method} failed"))
            return message

    async def _send_request(self, websocket: Any, method: str, params: dict[str, Any]) -> int:
        async with self._send_lock:
            self._rpc_id += 1
            request_id = self._rpc_id
            await websocket.send(
                json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            )
            return request_id

    async def _collect_response(
        self,
        websocket: Any,
        buffered_events: list[dict[str, Any]],
        status_callback: StatusCallback | None,
    ) -> str:
        while True:
            message = buffered_events.pop(0) if buffered_events else await self._receive_json(websocket)
            if message.get("method") != "event":
                continue
            params = message.get("params") or {}
            if params.get("session_id") != self._session_id:
                continue
            event_type = params.get("type")
            payload = params.get("payload") or {}
            if event_type == "message.delta":
                # Hermes may stream intermediate tool payloads through this callback.
                # The completion event is the sole authoritative user-facing answer.
                continue
            if event_type == "message.complete":
                if self._cancel_requested:
                    raise HermesCancelled("Hermes request cancelled")
                if payload.get("status") not in {None, "complete"}:
                    raise HermesInvocationError(f"Hermes turn ended with status {payload.get('status')}")
                answer = str(payload.get("text") or "").strip()
                if not answer:
                    raise HermesInvocationError("Hermes returned no answer")
                if len(answer.encode("utf-8")) > HERMES_OUTPUT_LIMIT_BYTES:
                    await self.interrupt()
                    raise HermesInvocationError("Hermes output exceeded the 32 KiB limit")
                if answer.startswith(("{", "[")):
                    raise HermesInvocationError("Hermes returned raw tool output instead of a spoken answer")
                if self._followup_queued:
                    self._followup_queued = False
                    continue
                return answer
            elif event_type == "error":
                raise HermesInvocationError(str(payload.get("message") or "Hermes gateway error"))
            elif event_type in {"status.update", "tool.start", "tool.generating"} and status_callback is not None:
                status = str(payload.get("text") or payload.get("name") or "working")
                result = status_callback(status)
                if inspect.isawaitable(result):
                    await result
            elif event_type and event_type.endswith(".request"):
                await self.interrupt()
                raise HermesInvocationError(f"Hermes requested unsupported interactive input ({event_type})")

    @staticmethod
    async def _receive_json(websocket: Any) -> dict[str, Any]:
        raw = await websocket.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise HermesInvocationError("Hermes gateway returned a non-object message")
        return message

    def _write_connection_file(self) -> None:
        if self._stored_session_id is None:
            return
        payload = {
            "gateway_url": f"http://127.0.0.1:{self.port}",
            "websocket_url": self.websocket_url,
            "session_id": self._stored_session_id,
            "session_title": HERMES_SESSION_TITLE,
        }
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.connection_file.with_suffix(".tmp")
        descriptor = json.dumps(payload, indent=2) + "\n"
        file_descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                handle.write(descriptor)
            os.replace(temporary, self.connection_file)
            os.chmod(self.connection_file, 0o600)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        logger.info("Hermes native session connection file ready at %s", self.connection_file)

    def _fetch_messages_blocking(self) -> dict[str, Any]:
        session_id = self._stored_session_id
        if session_id is None:
            return {"messages": []}
        url = (
            f"http://127.0.0.1:{self.port}/api/sessions/"
            f"{quote(session_id, safe='')}/messages?limit=500"
        )
        request = Request(
            url,
            headers={"X-Hermes-Session-Token": self._token},
        )
        with urlopen(request, timeout=5) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise HermesInvocationError("Hermes message history returned a non-object response")
        return payload

    async def _drain_stream(self, stream: Any) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            remaining = HERMES_OUTPUT_LIMIT_BYTES - total
            if remaining > 0:
                chunks.append(chunk[:remaining])
            total += len(chunk)
        return b"".join(chunks), total > HERMES_OUTPUT_LIMIT_BYTES

    @staticmethod
    def _startup_detail(output: bytearray) -> str:
        return bytes(output).decode("utf-8", errors="replace").strip()[-500:]

    async def _terminate_process(self, process: Any) -> None:
        if getattr(process, "returncode", None) is not None:
            return
        self._signal_process_group(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=HERMES_TERM_GRACE_SECONDS)
        except TimeoutError:
            self._signal_process_group(process, signal.SIGKILL)
            await process.wait()

    @staticmethod
    def _signal_process_group(process: Any, signum: int) -> None:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signum)
                return
            except ProcessLookupError:
                return
            except OSError as exc:
                logger.debug("Hermes process-group signal failed: %s", exc)
        sender = getattr(process, "send_signal", None)
        if callable(sender):
            sender(signum)


__all__ = [
    "HERMES_BINARY",
    "HERMES_GATEWAY_PORT",
    "HERMES_OUTPUT_LIMIT_BYTES",
    "HERMES_TOOLSETS",
    "HermesCancelled",
    "HermesError",
    "HermesGatewayClient",
    "HermesInvocationError",
    "HermesMissingBinary",
    "SAFETY_PROMPT",
]
