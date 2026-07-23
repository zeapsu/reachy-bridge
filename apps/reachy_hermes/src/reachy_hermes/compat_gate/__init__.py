"""Rerunnable compatibility gate for Hermes still-image delivery."""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import os
import re
import secrets
import signal
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GATE_NAME = "hermes-vision-compat"
PROJECT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_BINARY = Path("~/.local/bin/hermes").expanduser()
READY_PATTERN = re.compile(rb"HERMES_BACKEND_READY\s+port=(\d+)")
PLUGIN_PATTERN = re.compile(
    rb"Plugin reachy_probe registered tool: reachy_probe_snapshot"
)
OUTPUT_LIMIT_BYTES = 256 * 1024
TERM_TIMEOUT_SECONDS = 3.0
EXPECTED_COLOR = "blue"
EXPECTED_WORD = "REACHY"
FAIL_CLOSED_MARKER = "reachy_probe_snapshot denied: one-shot token already consumed"


def _timeout(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


STARTUP_TIMEOUT_SECONDS = _timeout("REACHY_HERMES_GATE_STARTUP_TIMEOUT", 25.0)
TURN_TIMEOUT_SECONDS = _timeout("REACHY_HERMES_GATE_TURN_TIMEOUT", 300.0)
WS_IO_TIMEOUT_SECONDS = _timeout("REACHY_HERMES_GATE_WS_TIMEOUT", 60.0)


def parse_build_identity(output: str) -> dict[str, str]:
    """Parse the public ``hermes --version`` identity, including local build suffixes."""
    first_line = re.search(
        r"^Hermes Agent v(?P<version>\d+\.\d+\.\d+)"
        r"\s+\((?P<date>[^)]+)\)\s+·\s+upstream\s+(?P<upstream>[0-9a-fA-F]+)"
        r"(?:\s+·\s+local\s+(?P<local>[0-9a-fA-F]+)"
        r"(?:\s+\(\+\d+\s+carried commits\))?)?\s*$",
        output,
        re.MULTILINE,
    )
    install = re.search(
        r"^Install directory:\s*(?P<path>.+?)\s*$", output, re.MULTILINE
    )
    if first_line is None or install is None:
        raise ValueError("unparseable Hermes build identity")
    identity = {
        "version": first_line.group("version"),
        "build_date": first_line.group("date"),
        "upstream_sha": first_line.group("upstream"),
        "install_dir": install.group("path"),
    }
    if first_line.group("local"):
        identity["local_sha"] = first_line.group("local")
    return identity


def make_check(check_id: str, status: str, evidence: Any) -> dict[str, Any]:
    if status not in {"pass", "fail", "skip"}:
        raise ValueError(f"invalid check status: {status}")
    return {"id": check_id, "status": status, "evidence": evidence}


def assemble_result(
    hermes: dict[str, Any], checks: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build the stable machine-readable result envelope."""
    return {
        "gate": GATE_NAME,
        "hermes": hermes,
        "checks": checks,
        "go": all(
            check["status"] == "pass" for check in checks if check["status"] != "skip"
        ),
    }


def _raw_hit(data: bytes, image_base64: str) -> str | None:
    if b"\xff\xd8\xff" in data:
        return "JPEG magic bytes"
    encoded = image_base64.encode("ascii")
    if len(encoded) >= 64:
        for index in range(len(encoded) - 63):
            if encoded[index : index + 64] in data:
                return "64-character probe image base64 substring"
    return None


def scan_for_probe_payload(
    roots: list[Path], blobs: list[tuple[str, bytes]], image_base64: str
) -> list[str]:
    """Find raw probe JPEG/base64 in files and captured process streams."""
    hits: list[str] = []
    for label, blob in blobs:
        hit = _raw_hit(blob, image_base64)
        if hit:
            hits.append(f"{label}: {hit}")
    for root in roots:
        paths = (
            [root] if root.is_file() else list(root.rglob("*")) if root.exists() else []
        )
        for path in paths:
            if not path.is_file():
                continue
            try:
                with path.open("rb") as handle:
                    carry = b""
                    while True:
                        chunk = handle.read(64 * 1024)
                        if not chunk:
                            break
                        sample = carry + chunk
                        hit = _raw_hit(sample, image_base64)
                        if hit:
                            hits.append(f"{path}: {hit}")
                            break
                        carry = sample[-(len(image_base64) + 2) :]
            except OSError as exc:
                hits.append(f"{path}: scan failed: {exc}")
    return hits


# ponytail: stat-keyed hash memo; unchanged (mtime_ns, size) means unchanged
# content. Ceiling: a mutation that preserves both stat fields would go
# unnoticed — acceptable here, upgrade to always-hash if that ever matters.
_STATE_HASH_CACHE: dict[str, tuple[int, int, str]] = {}


def _state_snapshot() -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    real_home = Path.home() / ".hermes"
    for name in ("state.db", "config.yaml"):
        path = real_home / name
        if not path.exists():
            snapshot[name] = {"present": False}
            continue
        try:
            stat = path.stat()
            cached = _STATE_HASH_CACHE.get(name)
            if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
                sha = cached[2]
            else:
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
                _STATE_HASH_CACHE[name] = (stat.st_mtime_ns, stat.st_size, sha)
            snapshot[name] = {
                "present": True,
                "sha256": sha,
                "mtime_ns": stat.st_mtime_ns,
            }
        except OSError as exc:
            snapshot[name] = {"present": True, "error": str(exc)}
    return snapshot


def _write_scratch_config(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - reachy_probe\n",
        encoding="utf-8",
    )


@dataclass
class _Capture:
    data: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    sensitive_base64: str = ""
    raw_hits: list[str] = field(default_factory=list)
    _scan_carry: bytes = b""

    def append(self, chunk: bytes) -> None:
        if self.sensitive_base64:
            sample = self._scan_carry + chunk
            hit = _raw_hit(sample, self.sensitive_base64)
            if hit and hit not in self.raw_hits:
                self.raw_hits.append(hit)
            self._scan_carry = sample[-(len(self.sensitive_base64) + 2) :]
        remaining = OUTPUT_LIMIT_BYTES - len(self.data)
        if remaining > 0:
            self.data.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.truncated = True

    def text_tail(self, limit: int = 1000) -> str:
        return bytes(self.data[-limit:]).decode("utf-8", errors="replace").strip()


@dataclass
class _ServeRuntime:
    process: asyncio.subprocess.Process
    stdout: _Capture
    stderr: _Capture
    tasks: list[asyncio.Task[None]]
    ready: asyncio.Event
    plugin_loaded: asyncio.Event
    matched_plugin_lines: list[str]
    token: str
    port: int | None = None

    @property
    def captured_blobs(self) -> list[tuple[str, bytes]]:
        return [
            ("hermes serve stdout", bytes(self.stdout.data)),
            ("hermes serve stderr", bytes(self.stderr.data)),
        ]


async def _drain_stream(
    stream: asyncio.StreamReader,
    capture: _Capture,
    runtime: _ServeRuntime | None = None,
) -> None:
    """Continuously drain one pipe while retaining only bounded evidence."""
    while True:
        line = await stream.readline()
        if not line:
            return
        capture.append(line)
        if runtime is None:
            continue
        ready_match = READY_PATTERN.search(line)
        if ready_match:
            runtime.port = int(ready_match.group(1))
            runtime.ready.set()
        if PLUGIN_PATTERN.search(line):
            decoded = line.decode("utf-8", errors="replace").strip()
            if decoded not in runtime.matched_plugin_lines:
                runtime.matched_plugin_lines.append(decoded)
            runtime.plugin_loaded.set()


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(
            asyncio.shield(process.wait()), timeout=TERM_TIMEOUT_SECONDS
        )
        return
    except TimeoutError:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(
            asyncio.shield(process.wait()), timeout=TERM_TIMEOUT_SECONDS
        )
    except TimeoutError:
        return


async def _wait_for_runtime_event(
    runtime: _ServeRuntime,
    event: asyncio.Event,
    timeout: float,
    description: str,
) -> None:
    event_task = asyncio.create_task(event.wait())
    exit_task = asyncio.create_task(runtime.process.wait())
    try:
        async with asyncio.timeout(timeout):
            done, _ = await asyncio.wait(
                {event_task, exit_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if event_task in done and event.is_set():
                return
            raise RuntimeError(
                f"Hermes exited with code {runtime.process.returncode} before {description}"
            )
    except TimeoutError as exc:
        raise RuntimeError(
            f"timed out after {timeout:.0f}s waiting for {description}"
        ) from exc
    finally:
        for task in (event_task, exit_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(event_task, exit_task, return_exceptions=True)


def _serve_environment(
    home: Path, *, audit_file: Path, nonce_file: Path, token: str
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "HERMES_HOME": str(home),
            "HERMES_ENABLE_PROJECT_PLUGINS": "1",
            "HERMES_PLUGINS_DEBUG": "1",
            "HERMES_TUI_TOOLSETS": "reachy_probe",
            "HERMES_DASHBOARD_SESSION_TOKEN": token,
            "REACHY_PROBE_AUDIT_FILE": str(audit_file),
            "REACHY_PROBE_NONCE_FILE": str(nonce_file),
        }
    )
    return environment


async def _start_serve(binary: Path, home: Path) -> _ServeRuntime:
    audit_file = home / "reachy-probe-audit.txt"
    nonce_file = home / "reachy-probe-nonce"
    token = secrets.token_urlsafe(32)
    process = await asyncio.create_subprocess_exec(
        str(binary),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--skip-build",
        "--isolated",
        cwd=PROJECT_DIR,
        env=_serve_environment(
            home, audit_file=audit_file, nonce_file=nonce_file, token=token
        ),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        await _terminate_process(process)
        raise RuntimeError("Hermes serve did not expose stdout/stderr pipes")
    runtime = _ServeRuntime(
        process=process,
        stdout=_Capture(sensitive_base64=_load_probe_base64()),
        stderr=_Capture(sensitive_base64=_load_probe_base64()),
        tasks=[],
        ready=asyncio.Event(),
        plugin_loaded=asyncio.Event(),
        matched_plugin_lines=[],
        token=token,
    )
    runtime.tasks = [
        asyncio.create_task(_drain_stream(process.stdout, runtime.stdout, runtime)),
        asyncio.create_task(_drain_stream(process.stderr, runtime.stderr, runtime)),
    ]
    return runtime


async def _close_runtime(runtime: _ServeRuntime) -> None:
    try:
        await _terminate_process(runtime.process)
    finally:
        try:
            async with asyncio.timeout(TERM_TIMEOUT_SECONDS):
                await asyncio.gather(*runtime.tasks, return_exceptions=True)
        except TimeoutError:
            for task in runtime.tasks:
                task.cancel()
            await asyncio.gather(*runtime.tasks, return_exceptions=True)


async def _run_version(
    binary: Path, *, environment: dict[str, str] | None = None
) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        str(binary),
        "--version",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        env=environment,
    )
    if process.stdout is None or process.stderr is None:
        await _terminate_process(process)
        raise RuntimeError("Hermes --version did not expose stdout/stderr pipes")
    stdout = _Capture()
    stderr = _Capture()
    tasks = [
        asyncio.create_task(_drain_stream(process.stdout, stdout)),
        asyncio.create_task(_drain_stream(process.stderr, stderr)),
    ]
    try:
        async with asyncio.timeout(STARTUP_TIMEOUT_SECONDS):
            returncode = await process.wait()
    except TimeoutError:
        await _terminate_process(process)
        raise RuntimeError("Hermes --version timed out") from None
    finally:
        try:
            async with asyncio.timeout(TERM_TIMEOUT_SECONDS):
                await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    return int(returncode), bytes(stdout.data), bytes(stderr.data)


async def _check_build_identity(binary: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not binary.is_file():
        return make_check(
            "build_identity", "fail", f"Hermes executable not found at {binary}"
        ), {}
    try:
        returncode, stdout, stderr = await _run_version(binary)
        if returncode != 0:
            detail = (stderr or stdout).decode("utf-8", errors="replace")[-1000:]
            raise RuntimeError(f"hermes --version exited {returncode}: {detail}")
        identity = parse_build_identity(stdout.decode("utf-8", errors="replace"))
        identity["binary"] = str(binary)
        return make_check("build_identity", "pass", identity.copy()), identity
    except Exception as exc:
        return make_check("build_identity", "fail", str(exc)), {}


async def _check_plugin_discovery(binary: Path) -> dict[str, Any]:
    if not binary.is_file():
        return make_check(
            "plugin_discovery", "fail", f"Hermes executable not found at {binary}"
        )
    runtime: _ServeRuntime | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="reachy-hermes-discovery-"
        ) as temporary:
            home = Path(temporary)
            _write_scratch_config(home)
            runtime = await _start_serve(binary, home)
            await _wait_for_runtime_event(
                runtime,
                runtime.plugin_loaded,
                STARTUP_TIMEOUT_SECONDS,
                "reachy_probe registration debug line",
            )
            return make_check(
                "plugin_discovery",
                "pass",
                {
                    "matched_log_lines": runtime.matched_plugin_lines,
                    "method": "hermes serve runtime discovery",
                },
            )
    except Exception as exc:
        evidence: dict[str, Any] = {"error": str(exc)}
        if runtime is not None:
            evidence["stdout_tail"] = runtime.stdout.text_tail()
            evidence["stderr_tail"] = runtime.stderr.text_tail()
        return make_check("plugin_discovery", "fail", evidence)
    finally:
        if runtime is not None:
            await _close_runtime(runtime)


class _GatewayProbe:
    def __init__(self, port: int, token: str) -> None:
        self.url = f"ws://127.0.0.1:{port}/api/ws?token={token}"
        self.request_id = 0
        self.events: list[dict[str, Any]] = []
        self.event_types: list[str] = []
        self.sent_methods: list[str] = []
        self.session_info: dict[str, Any] = {}

    def _record_event(self, message: dict[str, Any]) -> None:
        if message.get("method") != "event":
            return
        self.events.append(message)
        event_type = str((message.get("params") or {}).get("type") or "event")
        self.event_types.append(event_type)

    async def _recv(self, websocket: Any) -> dict[str, Any]:
        try:
            async with asyncio.timeout(WS_IO_TIMEOUT_SECONDS):
                raw = await websocket.recv()
        except TimeoutError as exc:
            raise RuntimeError(
                f"WebSocket receive timed out after {WS_IO_TIMEOUT_SECONDS:.0f}s"
            ) from exc
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise RuntimeError("gateway returned a non-object JSON-RPC message")
        return message

    async def _send(self, websocket: Any, method: str, params: dict[str, Any]) -> int:
        self.request_id += 1
        request_id = self.request_id
        try:
            async with asyncio.timeout(WS_IO_TIMEOUT_SECONDS):
                await websocket.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": method,
                            "params": params,
                        }
                    )
                )
        except TimeoutError as exc:
            raise RuntimeError(
                f"WebSocket send timed out after {WS_IO_TIMEOUT_SECONDS:.0f}s"
            ) from exc
        self.sent_methods.append(method)
        return request_id

    async def _rpc(
        self, websocket: Any, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        request_id = await self._send(websocket, method, params)
        while True:
            message = await self._recv(websocket)
            if message.get("method") == "event":
                self._record_event(message)
                continue
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                error = message["error"]
                raise RuntimeError(
                    str(error.get("message") if isinstance(error, dict) else error)
                )
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    async def run_turn(self, prompt: str) -> dict[str, Any]:
        from websockets.asyncio.client import connect

        try:
            async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
                async with connect(
                    self.url,
                    open_timeout=WS_IO_TIMEOUT_SECONDS,
                    close_timeout=WS_IO_TIMEOUT_SECONDS,
                ) as websocket:
                    while True:
                        message = await self._recv(websocket)
                        self._record_event(message)
                        if (
                            message.get("method") == "event"
                            and (message.get("params") or {}).get("type")
                            == "gateway.ready"
                        ):
                            break
                    self.session_info = await self._rpc(
                        websocket,
                        "session.create",
                        {
                            "cwd": str(PROJECT_DIR),
                            "title": "reachy-vision-compat-gate",
                            "source": "reachy",
                            "messages": [],
                        },
                    )
                    session_id = self.session_info.get("session_id")
                    if not isinstance(session_id, str) or not session_id:
                        raise RuntimeError("session.create returned no session_id")
                    await self._rpc(
                        websocket,
                        "config.set",
                        {
                            "session_id": session_id,
                            "key": "yolo",
                            "value": "on",
                            "scope": "session",
                        },
                    )
                    await self._rpc(
                        websocket,
                        "prompt.submit",
                        {"session_id": session_id, "text": prompt},
                    )
                    completion = self._completion()
                    while completion is None:
                        message = await self._recv(websocket)
                        self._record_event(message)
                        completion = self._completion()
                    return completion
        except TimeoutError as exc:
            raise RuntimeError(
                f"Hermes turn timed out after {TURN_TIMEOUT_SECONDS:.0f}s"
            ) from exc

    def _completion(self) -> dict[str, Any] | None:
        for message in reversed(self.events):
            params = message.get("params") or {}
            if params.get("type") != "message.complete":
                continue
            payload = params.get("payload")
            if isinstance(payload, dict):
                return payload
        return None


def _event_payloads(probe: _GatewayProbe, event_type: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for event in probe.events:
        params = event.get("params") or {}
        if params.get("type") != event_type:
            continue
        payload = params.get("payload")
        payloads.append(payload if isinstance(payload, dict) else {})
    return payloads


def _profile_values(value: Any) -> tuple[str | None, str | None]:
    provider: str | None = None
    model: str | None = None
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).casefold()
            if (
                provider is None
                and lowered in {"provider", "provider_name"}
                and isinstance(child, str)
            ):
                provider = child
            if (
                model is None
                and lowered in {"model", "model_name", "model_id"}
                and isinstance(child, str)
            ):
                model = child
            nested_provider, nested_model = _profile_values(child)
            provider = provider or nested_provider
            model = model or nested_model
    elif isinstance(value, list):
        for child in value:
            nested_provider, nested_model = _profile_values(child)
            provider = provider or nested_provider
            model = model or nested_model
    return provider, model


def _delivery_mode(completion_text: str) -> str:
    lowered = completion_text.casefold()
    if EXPECTED_COLOR in lowered and EXPECTED_WORD.casefold() in lowered:
        return "native"
    if any(
        marker in lowered
        for marker in (
            "text-only",
            "only a text",
            "no image was included",
            "no actual image",
            "no image to inspect",
        )
    ):
        return "text-downgraded"
    return "unverified"


@functools.lru_cache(maxsize=1)
def _load_probe_base64() -> str:
    plugin_path = PROJECT_DIR / ".hermes/plugins/reachy_probe/__init__.py"
    namespace: dict[str, Any] = {}
    exec(
        compile(plugin_path.read_text(encoding="utf-8"), str(plugin_path), "exec"),
        namespace,
    )
    image_base64 = namespace.get("IMAGE_BASE64")
    if not isinstance(image_base64, str):
        raise RuntimeError("probe plugin does not expose IMAGE_BASE64")
    base64.b64decode(image_base64, validate=True)
    return image_base64


def _in_turn_image_errors(
    *, image_deliveries: int, prompt_count: int, status: Any, text: str
) -> list[str]:
    """Assert exactly one image envelope entered a single-prompt turn.

    A model may retry a failed/denied call, so the tool.start count is not
    asserted — what matters is exactly one image delivery reached the turn.
    """
    errors: list[str] = []
    if image_deliveries != 1:
        errors.append(f"expected exactly one image delivery, observed {image_deliveries}")
    if prompt_count != 1:
        errors.append(f"expected one prompt.submit, observed {prompt_count}")
    if status != "complete":
        errors.append(f"message.complete status={status!r}")
    if EXPECTED_COLOR not in text.casefold():
        errors.append(f"completion did not contain {EXPECTED_COLOR!r}")
    if EXPECTED_WORD.casefold() not in text.casefold():
        errors.append(f"completion did not contain {EXPECTED_WORD!r}")
    return errors


def _single_use_errors(
    *,
    tool_invocations: int,
    audit_delta: list[str],
    complete_text: str,
    completion_text: str,
    nonce_exists: bool,
) -> list[str]:
    """Assert the probe returned one image then failed closed on every retry.

    Contract: at least two tool invocations, audit delta is a single ``image``
    followed by one-or-more ``denied`` (which subsumes "exactly one image
    envelope observed"), the fail-closed marker surfaced, and the one-shot
    nonce is gone.
    """
    errors: list[str] = []
    if tool_invocations < 2:
        errors.append(
            f"expected at least two tool invocations, observed {tool_invocations}"
        )
    if (
        FAIL_CLOSED_MARKER not in complete_text
        and FAIL_CLOSED_MARKER not in completion_text
    ):
        errors.append("second tool result did not expose the fail-closed marker")
    fail_closed_shape = (
        len(audit_delta) >= 2
        and audit_delta[0] == "image"
        and all(entry == "denied" for entry in audit_delta[1:])
    )
    if not fail_closed_shape:
        errors.append(
            f"expected audit ['image', 'denied', ...], observed {audit_delta!r}"
        )
    if nonce_exists:
        errors.append("one-shot nonce still exists after first tool use")
    return errors


def _model_profile_result(
    provider: str | None, model: str | None, completion_text: str
) -> dict[str, Any]:
    """Record provider/model + delivery mode without gating on missing metadata.

    "Record, don't guess": absent provider/model keys are noted as ``unknown``,
    never a failure. Only a delivery mode we could not classify (``unverified``)
    fails — and in_turn_image already fails alongside it, so this check is never
    the sole cause of ``go=false``.
    """
    recognized = (
        EXPECTED_COLOR in completion_text.casefold()
        and EXPECTED_WORD.casefold() in completion_text.casefold()
    )
    delivery_mode = _delivery_mode(completion_text)
    downgraded = delivery_mode == "text-downgraded"
    metadata_not_found = [
        name for name, value in (("provider", provider), ("model", model)) if not value
    ]
    classified = delivery_mode != "unverified"
    return make_check(
        "model_profile",
        "pass" if classified else "fail",
        {
            "provider": provider or "unknown",
            "model": model or "unknown",
            "delivery_mode": delivery_mode,
            "basis": (
                "model named both image-only test values after one multimodal tool call"
                if recognized
                else "model explicitly reported text-only image delivery"
                if downgraded
                else "completion did not prove native image inspection"
            ),
            "metadata_not_found": metadata_not_found,
            "errors": []
            if classified
            else ["delivery mode unverified: image delivery could not be classified"],
        },
    )


def _persistence_check(home: Path, runtime: _ServeRuntime | None) -> dict[str, Any]:
    """Scan scratch home + drained stream hits for raw probe payload.

    Any failure here yields a failing no_raw_persistence check rather than an
    escaping exception, so already-recorded live checks are never discarded by
    run_gate's outer handler. Stream content is covered by the incremental
    ``_Capture`` scan (``raw_hits``); only on-disk files need a fresh scan.
    """
    try:
        image_base64 = _load_probe_base64()
        hits = scan_for_probe_payload([home], [], image_base64)
        if runtime is not None:
            for label, capture in (
                ("hermes serve stdout", runtime.stdout),
                ("hermes serve stderr", runtime.stderr),
            ):
                hits.extend(f"{label}: {hit}" for hit in capture.raw_hits)
    except Exception as exc:
        return make_check(
            "no_raw_persistence",
            "fail",
            {"error": f"persistence scan failed: {exc}"},
        )
    return make_check(
        "no_raw_persistence",
        "fail" if hits else "pass",
        hits
        or {
            "scanned": [
                "scratch HERMES_HOME recursively",
                "full drained Hermes serve stdout/stderr",
            ],
            "hits": 0,
        },
    )


async def _run_live_checks(
    binary: Path, state_audit: Callable[[str], None] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []

    def add(check: dict[str, Any]) -> None:
        checks.append(check)
        if state_audit is not None:
            state_audit(check["id"])

    temporary = tempfile.TemporaryDirectory(prefix="reachy-hermes-live-")
    home = Path(temporary.name)
    _write_scratch_config(home)
    runtime: _ServeRuntime | None = None
    plugin_check: dict[str, Any] | None = None
    first_probe: _GatewayProbe | None = None
    first_completion: dict[str, Any] | None = None
    try:
        try:
            runtime = await _start_serve(binary, home)
            await _wait_for_runtime_event(
                runtime, runtime.ready, STARTUP_TIMEOUT_SECONDS, "gateway ready marker"
            )
            await _wait_for_runtime_event(
                runtime,
                runtime.plugin_loaded,
                STARTUP_TIMEOUT_SECONDS,
                "reachy_probe registration debug line",
            )
            # Derive plugin_discovery from this shared live serve rather than
            # starting a second discovery serve.
            plugin_check = make_check(
                "plugin_discovery",
                "pass",
                {
                    "matched_log_lines": runtime.matched_plugin_lines,
                    "method": "shared live hermes serve runtime discovery",
                },
            )
            add(
                make_check(
                    "serve_isolated",
                    "pass",
                    {
                        "port": runtime.port,
                        "hermes_home": "scratch",
                        "plugin_log_lines": runtime.matched_plugin_lines,
                    },
                )
            )
        except Exception as exc:
            evidence: dict[str, Any] = {"error": str(exc)}
            if runtime is not None:
                evidence["stdout_tail"] = runtime.stdout.text_tail()
                evidence["stderr_tail"] = runtime.stderr.text_tail()
            plugin_check = make_check("plugin_discovery", "fail", evidence)
            add(make_check("serve_isolated", "fail", evidence))
            for check_id in ("in_turn_image", "single_use"):
                add(
                    make_check(
                        check_id, "fail", "isolated Hermes serve did not become usable"
                    )
                )
            if runtime is not None:
                await _close_runtime(runtime)
            add(_persistence_check(home, runtime))
            runtime = None
            add(
                make_check(
                    "model_profile",
                    "fail",
                    "no provider/model or delivery mode observed because serve failed",
                )
            )
            return plugin_check, checks

        nonce_file = home / "reachy-probe-nonce"
        audit_file = home / "reachy-probe-audit.txt"
        nonce_file.write_text("in-turn-image\n", encoding="utf-8")
        first_probe = _GatewayProbe(int(runtime.port or 0), runtime.token)
        try:
            first_completion = await first_probe.run_turn(
                "Call reachy_probe_snapshot exactly once. Inspect the image in this current turn, "
                "then state the dominant color and the word visible in the image."
            )
            text = str(first_completion.get("text") or "")
            starts = _event_payloads(first_probe, "tool.start")
            prompt_count = first_probe.sent_methods.count("prompt.submit")
            audit_lines = (
                audit_file.read_text(encoding="utf-8").splitlines()
                if audit_file.exists()
                else []
            )
            image_deliveries = audit_lines.count("image")
            errors = _in_turn_image_errors(
                image_deliveries=image_deliveries,
                prompt_count=prompt_count,
                status=first_completion.get("status"),
                text=text,
            )
            add(
                make_check(
                    "in_turn_image",
                    "fail" if errors else "pass",
                    {
                        "errors": errors,
                        "event_types": first_probe.event_types,
                        "prompt_submit_count": prompt_count,
                        "tool_invocation_count": len(starts),
                        "image_delivery_count": image_deliveries,
                        "completion_text": text,
                    },
                )
            )
        except Exception as exc:
            add(
                make_check(
                    "in_turn_image",
                    "fail",
                    {
                        "error": str(exc),
                        "event_types": first_probe.event_types,
                        "hint": "no provider configured under scratch HERMES_HOME"
                        if not first_probe._completion()
                        else "turn failed before a valid completion",
                    },
                )
            )

        nonce_file.write_text("single-use\n", encoding="utf-8")
        before_audit = (
            audit_file.read_text(encoding="utf-8").splitlines()
            if audit_file.exists()
            else []
        )
        second_probe = _GatewayProbe(int(runtime.port or 0), runtime.token)
        try:
            second_completion = await second_probe.run_turn(
                "Call reachy_probe_snapshot twice in this one turn. You must make two separate tool "
                "calls even if the first succeeds, then report the result of each call."
            )
            after_audit = (
                audit_file.read_text(encoding="utf-8").splitlines()
                if audit_file.exists()
                else []
            )
            audit_delta = after_audit[len(before_audit) :]
            starts = _event_payloads(second_probe, "tool.start")
            completes = _event_payloads(second_probe, "tool.complete")
            complete_text = json.dumps(completes, sort_keys=True)
            errors = _single_use_errors(
                tool_invocations=len(starts),
                audit_delta=audit_delta,
                complete_text=complete_text,
                completion_text=str(second_completion.get("text") or ""),
                nonce_exists=nonce_file.exists(),
            )
            add(
                make_check(
                    "single_use",
                    "fail" if errors else "pass",
                    {
                        "errors": errors,
                        "event_types": second_probe.event_types,
                        "tool_invocation_count": len(starts),
                        "audit": audit_delta,
                    },
                )
            )
        except Exception as exc:
            add(
                make_check(
                    "single_use",
                    "fail",
                    {
                        "error": str(exc),
                        "event_types": second_probe.event_types,
                        "hint": "model response unavailable; single-use behavior was not proven",
                    },
                )
            )

        await _close_runtime(runtime)
        add(_persistence_check(home, runtime))
        runtime = None

        if first_probe is None or first_completion is None:
            add(
                make_check(
                    "model_profile",
                    "fail",
                    "no completed image turn; provider/model and delivery mode are unverified",
                )
            )
        else:
            provider, model = _profile_values(
                {
                    "completion": first_completion,
                    "session": first_probe.session_info,
                    "events": first_probe.events,
                }
            )
            add(
                _model_profile_result(
                    provider, model, str(first_completion.get("text") or "")
                )
            )
        return plugin_check, checks
    finally:
        if runtime is not None:
            await _close_runtime(runtime)
        temporary.cleanup()


async def run_gate(*, live: bool) -> dict[str, Any]:
    """Run every gate check without allowing a named failure to escape as a traceback."""
    binary = Path(
        os.environ.get("REACHY_HERMES_BINARY", str(DEFAULT_BINARY))
    ).expanduser()
    original_state = _state_snapshot()
    mutations: list[dict[str, Any]] = []

    def audit_state(after_check: str) -> None:
        current = _state_snapshot()
        if current != original_state:
            mutations.append(
                {"after_check": after_check, "before": original_state, "after": current}
            )

    checks: list[dict[str, Any]] = []
    build_check, identity = await _check_build_identity(binary)
    checks.append(build_check)
    audit_state("build_identity")

    if live:
        # The live serve already waits for the plugin_loaded log line, so
        # plugin_discovery is derived from it instead of starting a second
        # discovery serve.
        try:
            plugin_check, live_checks = await _run_live_checks(binary, audit_state)
        except Exception as exc:
            plugin_check = make_check(
                "plugin_discovery", "fail", f"unexpected live setup failure: {exc}"
            )
            live_checks = [
                make_check(
                    "serve_isolated", "fail", f"unexpected live setup failure: {exc}"
                ),
                make_check("in_turn_image", "fail", "live setup failed"),
                make_check("single_use", "fail", "live setup failed"),
                make_check(
                    "no_raw_persistence", "fail", "live scan could not complete"
                ),
                make_check("model_profile", "fail", "live setup failed"),
            ]
            for check in live_checks:
                audit_state(check["id"])
    else:
        plugin_check = await _check_plugin_discovery(binary)
        reason = "not run without --live"
        live_checks = [
            make_check(check_id, "skip", reason)
            for check_id in (
                "serve_isolated",
                "in_turn_image",
                "single_use",
                "no_raw_persistence",
                "model_profile",
            )
        ]

    checks.append(plugin_check)
    audit_state("plugin_discovery")

    state_placeholder_index = len(checks)
    checks.append(make_check("state_isolation", "pass", {}))

    for check in live_checks:
        checks.append(check)
        if not live:
            audit_state(check["id"])

    final_state = _state_snapshot()
    if final_state != original_state and not mutations:
        mutations.append(
            {"after_check": "final", "before": original_state, "after": final_state}
        )
    checks[state_placeholder_index] = make_check(
        "state_isolation",
        "fail" if mutations else "pass",
        mutations
        or {
            "paths": ["~/.hermes/state.db", "~/.hermes/config.yaml"],
            "result": "hash and mtime unchanged (or files remained absent)",
        },
    )
    return assemble_result(identity, checks)


__all__ = [
    "EXPECTED_COLOR",
    "EXPECTED_WORD",
    "FAIL_CLOSED_MARKER",
    "assemble_result",
    "make_check",
    "parse_build_identity",
    "run_gate",
    "scan_for_probe_payload",
]
