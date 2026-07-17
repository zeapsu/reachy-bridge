"""Hermes one-shot voice handler.

Hermes is deliberately kept behind a small, read-only subprocess boundary. The
Reachy chat package still owns microphone capture, local Whisper, local TTS,
and movement/listening lifecycle; this module only replaces the cloud completion
step with the local Hermes executable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeAlias

from fastrtc import AdditionalOutputs
from reachy_chat.openrouter_chat import OpenRouterChatHandler


logger = logging.getLogger(__name__)

HERMES_BINARY = Path("~/.local/bin/hermes").expanduser()
HERMES_TOOLSETS = "web,session_search"
HERMES_TIMEOUT_SECONDS = 180.0
WORKING_UPDATE_INITIAL_DELAY_SECONDS = 30.0
WORKING_UPDATE_INTERVAL_SECONDS = 60.0
WORKING_UPDATE_MESSAGE = "I'm still working on that."
HERMES_TERM_GRACE_SECONDS = 3.0
HERMES_OUTPUT_LIMIT_BYTES = 32 * 1024
HISTORY_MAX_MESSAGES = 10
HISTORY_MAX_BYTES = 12 * 1024

CANCEL_REPLY = "Okay, I stopped."
MISSING_BINARY_REPLY = "I can't start Hermes because its executable is missing."
FAILURE_REPLY = "I couldn't complete that request because Hermes failed."

SAFETY_PROMPT = """You are Hermes, a read-only voice assistant for a Reachy Mini robot.
Answer the user's request concisely and clearly.
You may search the web and read prior sessions through the fixed toolset allowlist.
Never mutate files, memory, sessions, settings, accounts, websites, devices, or robot state.
Never send messages, submit forms, make purchases, run commands, install software, or cause any other side effect.
If a request would mutate state or cause a side effect, refuse that part and offer a read-only alternative.
Do not use a camera or claim to see anything; no camera is available to this app.
"""

_CANCEL_PATTERN = re.compile(r"\b(?:stop|cancel|never\s+mind)\b", re.IGNORECASE)

ProcessFactory: TypeAlias = Callable[..., Awaitable[Any]]


class HermesError(RuntimeError):
    """Base class for user-visible Hermes failures."""


class HermesMissingBinary(HermesError):
    """The fixed Hermes executable is not installed."""


class HermesInvocationError(HermesError):
    """Hermes exited unsuccessfully or produced invalid output."""


class HermesTimeout(HermesError):
    """Hermes exceeded its bounded wall-clock timeout."""


class HermesCancelled(HermesError):
    """The user asked the current Hermes request to stop."""


def is_cancel_phrase(transcript: str) -> bool:
    """Return whether speech contains one of the supported stop phrases."""
    normalized = re.sub(r"[^a-z]+", " ", transcript.casefold()).strip()
    return _CANCEL_PATTERN.search(normalized) is not None


def _clip_utf8(text: str, maximum: int) -> str:
    """Clip text to a UTF-8 byte limit without splitting a code point."""
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum:
        return text
    return encoded[:maximum].decode("utf-8", errors="ignore")


class HermesHandler(OpenRouterChatHandler):
    """Local Whisper/TTS handler whose completion step invokes Hermes once."""

    MAX_HISTORY_MESSAGES = HISTORY_MAX_MESSAGES
    HISTORY_MAX_BYTES = HISTORY_MAX_BYTES
    HERMES_TIMEOUT_SECONDS = HERMES_TIMEOUT_SECONDS
    HERMES_OUTPUT_LIMIT_BYTES = HERMES_OUTPUT_LIMIT_BYTES
    WORKING_UPDATE_INITIAL_DELAY_SECONDS = WORKING_UPDATE_INITIAL_DELAY_SECONDS
    WORKING_UPDATE_INTERVAL_SECONDS = WORKING_UPDATE_INTERVAL_SECONDS
    WORKING_UPDATE_MESSAGE = WORKING_UPDATE_MESSAGE
    SYSTEM_PROMPT = SAFETY_PROMPT
    FALLBACK_REPLY = FAILURE_REPLY

    def __init__(
        self,
        deps: Any,
        instance_path: str | None = None,
        startup_voice: str | None = None,
        *,
        hermes_binary: str | Path = HERMES_BINARY,
        process_factory: ProcessFactory | None = None,
    ) -> None:
        """Construct lazily; no Hermes process, camera, or speech engine starts here."""
        super().__init__(deps, instance_path=instance_path, startup_voice=startup_voice)
        self.hermes_binary = Path(hermes_binary).expanduser()
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._active_process: Any | None = None
        self._working_status_task: asyncio.Task[None] | None = None
        self._cancel_requested = False
        self._cancel_detection_task: asyncio.Task[None] | None = None
        self._cancel_speech_active = False
        self._cancel_speech_chunks: list[Any] = []
        self._cancel_speech_samples = 0
        self._cancel_silence_samples = 0

    def copy(self) -> "HermesHandler":
        """Create a fresh handler while preserving the selected voice and binary."""
        return HermesHandler(
            self.deps,
            self.instance_path,
            startup_voice=self._voice_override,
            hermes_binary=self.hermes_binary,
            process_factory=self._process_factory,
        )

    async def start_up(self) -> None:
        """Keep LocalStream's startup generation alive without a cloud client."""
        await self.run()

    async def run(self) -> None:
        """Expose a connected local handler until LocalStream requests shutdown."""
        self._stop_event.clear()
        self.connection = self
        self._mark_activity("hermes_ready")
        try:
            await self._stop_event.wait()
        finally:
            self.connection = None

    async def shutdown(self) -> None:
        """Stop cancellation detection, terminate Hermes, then release local audio."""
        self._stop_event.set()
        detection_task = self._cancel_detection_task
        if detection_task is not None and detection_task is not asyncio.current_task() and not detection_task.done():
            detection_task.cancel()
            await asyncio.gather(detection_task, return_exceptions=True)
        self._cancel_detection_task = None
        await self._stop_working_status_updates()
        process = self._active_process
        if process is not None:
            self._cancel_requested = True
            await self._terminate_process(process)
        await super().shutdown()

    async def receive(self, frame: Any) -> None:
        """Continue listening for a spoken cancellation while Hermes is working."""
        if not self._processing_turn:
            await super().receive(frame)
            return
        self._receive_cancel_frame(frame)

    def _receive_cancel_frame(self, frame: Any) -> None:
        """Buffer a second speech stream used only to recognize cancellation."""
        sample_rate, samples = frame
        audio = self._prepare_audio(samples, sample_rate)
        if audio.size == 0:
            return
        energy = float((audio * audio).mean() ** 0.5)
        is_speech = energy >= self.SPEECH_ENERGY_THRESHOLD
        if is_speech:
            if not self._cancel_speech_active:
                self._cancel_speech_active = True
                self._set_listening(True)
            self._cancel_speech_chunks.append(audio)
            self._cancel_speech_samples += audio.size
            self._cancel_silence_samples = 0
        elif self._cancel_speech_active:
            self._cancel_speech_chunks.append(audio)
            self._cancel_silence_samples += audio.size

        duration = sum(chunk.size for chunk in self._cancel_speech_chunks) / self.SAMPLE_RATE
        silence = self._cancel_silence_samples / self.SAMPLE_RATE
        if not self._cancel_speech_active or (silence < self.SILENCE_DURATION_S and duration < self.MAX_UTTERANCE_DURATION_S):
            return
        utterance = self._join_cancel_audio()
        self._reset_cancel_input()
        self._set_listening(False)
        if self._cancel_detection_task is None or self._cancel_detection_task.done():
            self._cancel_detection_task = asyncio.create_task(
                self._check_cancel_utterance(utterance),
                name="hermes-cancel-detection",
            )

    def _join_cancel_audio(self) -> Any:
        """Join buffered audio without touching the active turn's input buffer."""
        import numpy as np

        if not self._cancel_speech_chunks:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(self._cancel_speech_chunks)

    def _reset_cancel_input(self) -> None:
        """Clear only the cancellation detector's temporary audio state."""
        self._cancel_speech_active = False
        self._cancel_speech_chunks.clear()
        self._cancel_speech_samples = 0
        self._cancel_silence_samples = 0

    async def _check_cancel_utterance(self, audio: Any) -> None:
        """Transcribe the side-channel utterance and stop the active process if asked."""
        try:
            transcript = (await self._transcribe(audio)).strip()
        except Exception as exc:
            logger.debug("Hermes cancellation transcription failed: %s", exc)
            return
        if is_cancel_phrase(transcript):
            logger.info("Hermes request cancelled by voice phrase: %r", transcript)
            await self.cancel_current()

    async def cancel_current(self) -> None:
        """Terminate the active Hermes process after a spoken cancellation request."""
        self._cancel_requested = True
        await self._stop_working_status_updates()
        process = self._active_process
        if process is not None:
            await self._terminate_process(process)
        clear_queue = self._clear_queue
        if clear_queue is not None:
            try:
                clear_queue()
            except Exception:
                logger.debug("Unable to clear audio after Hermes cancellation", exc_info=True)

    def _start_working_status_updates(self) -> None:
        """Start one task for the initial 30-second and recurring 60-second cues."""
        task = self._working_status_task
        if task is not None and not task.done():
            return
        self._working_status_task = asyncio.create_task(
            self._working_status_updates(),
            name="hermes-working-status",
        )

    async def _stop_working_status_updates(self) -> None:
        """Cancel and await status speech before a final reply can be synthesized."""
        task = self._working_status_task
        self._working_status_task = None
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _working_status_updates(self) -> None:
        """Speak a first progress cue at 30 seconds, then repeat every 60 seconds."""
        try:
            await asyncio.sleep(self.WORKING_UPDATE_INITIAL_DELAY_SECONDS)
            while self._active_process is not None:
                await self._speak_working_update()
                await asyncio.sleep(self.WORKING_UPDATE_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Hermes working-status speech failed")

    async def _speak_working_update(self) -> None:
        """Queue one spoken progress cue and its event-log text."""
        message = self.WORKING_UPDATE_MESSAGE
        await self.output_queue.put(AdditionalOutputs({"role": "assistant", "content": message}))
        try:
            sample_rate, speech = await self._synthesize(message)
        except Exception:
            logger.exception("Hermes working-status TTS failed")
            return
        for offset in range(0, speech.size, self.OUTPUT_CHUNK_SAMPLES):
            await self.output_queue.put((sample_rate, speech[offset : offset + self.OUTPUT_CHUNK_SAMPLES]))
        if speech.size:
            await asyncio.sleep(speech.size / sample_rate)

    def _history_bytes(self, history: list[dict[str, str]] | None = None) -> int:
        """Return the serialized history size used by the Hermes prompt."""
        messages = self.conversation_history if history is None else history
        return sum(len(f"{message['role']}: {message['content']}\n".encode("utf-8")) for message in messages)

    def _record_message(self, role: Any, content: str) -> None:
        """Retain only recent history that fits both message and byte budgets."""
        clipped = _clip_utf8(content, self.HISTORY_MAX_BYTES)
        self.conversation_history.append({"role": role, "content": clipped})
        del self.conversation_history[:-self.MAX_HISTORY_MESSAGES]
        while len(self.conversation_history) > 1 and self._history_bytes() > self.HISTORY_MAX_BYTES:
            del self.conversation_history[0]
        if self.conversation_history and self._history_bytes() > self.HISTORY_MAX_BYTES:
            latest = self.conversation_history[-1]
            latest["content"] = _clip_utf8(
                latest["content"],
                max(0, self.HISTORY_MAX_BYTES - len(f"{latest['role']}: \n".encode("utf-8"))),
            )

    def build_prompt(self, prompt: str | None = None) -> str:
        """Build the read-only Hermes prompt with bounded conversation context."""
        lines = [self.SYSTEM_PROMPT.strip(), "", "Conversation history (read-only context):"]
        lines.extend(f"{message['role']}: {message['content']}" for message in self.conversation_history)
        if prompt is not None and (not self.conversation_history or self.conversation_history[-1]["content"] != prompt):
            lines.extend(("", "Current user request:", prompt))
        return "\n".join(lines)

    def build_command(self, prompt: str) -> list[str]:
        """Return the exact fixed Hermes one-shot command."""
        return [
            str(self.hermes_binary),
            "--oneshot",
            prompt,
            "--toolsets",
            HERMES_TOOLSETS,
        ]

    async def _request_completion(self, image_jpeg: bytes | None = None) -> str:
        """Invoke Hermes and turn every failure into clear spoken text."""
        del image_jpeg  # Hermes is intentionally camera-free.
        # discard any partial cancel utterance left by the preceding turn.
        self._reset_cancel_input()
        self._set_listening(False)
        self._cancel_requested = False
        self._start_working_status_updates()
        try:
            try:
                return await self.invoke_hermes()
            except HermesCancelled:
                return CANCEL_REPLY
            except HermesMissingBinary as exc:
                logger.error("Hermes executable missing: %s", exc)
                return MISSING_BINARY_REPLY
            except HermesError as exc:
                logger.error("Hermes request failed: %s", exc)
                return FAILURE_REPLY
        finally:
            # Await cancellation so a progress cue can never enqueue after final TTS starts.
            await self._stop_working_status_updates()
            self._reset_cancel_input()
            self._set_listening(False)

    async def invoke_hermes(self, prompt: str | None = None) -> str:
        """Run Hermes with bounded output drains and process-group cleanup."""
        # Hermes treats this worker marker as permission to inject mutating kanban tools.
        environment = os.environ.copy()
        environment.pop("HERMES_KANBAN_TASK", None)
        command = self.build_command(self.build_prompt(prompt))
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

        self._active_process = process
        wait_task = asyncio.create_task(process.wait(), name="hermes-process-wait")
        stdout_task = asyncio.create_task(self._drain_stream(process.stdout), name="hermes-stdout-drain")
        stderr_task = asyncio.create_task(self._drain_stream(process.stderr), name="hermes-stderr-drain")
        try:
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=self.HERMES_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as exc:
                await self._terminate_process(process)
                await wait_task
                raise HermesTimeout(f"Hermes timed out after {self.HERMES_TIMEOUT_SECONDS:.0f} seconds") from exc
            except asyncio.CancelledError:
                self._cancel_requested = True
                await self._terminate_process(process)
                await asyncio.gather(wait_task, return_exceptions=True)
                raise

            stdout_result, stderr_result = await asyncio.gather(stdout_task, stderr_task)
            stdout, stdout_overflow = stdout_result
            stderr, stderr_overflow = stderr_result
            if self._cancel_requested:
                raise HermesCancelled("Hermes request cancelled")
            if stdout_overflow or stderr_overflow:
                raise HermesInvocationError("Hermes output exceeded the 32 KiB limit")
            if process.returncode:
                detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
                suffix = f": {detail[:500]}" if detail else ""
                raise HermesInvocationError(f"Hermes exited with code {process.returncode}{suffix}")
            try:
                answer = stdout.decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise HermesInvocationError("Hermes returned invalid UTF-8 output") from exc
            if not answer:
                raise HermesInvocationError("Hermes returned no answer")
            return answer
        finally:
            if not stdout_task.done():
                await asyncio.gather(stdout_task, return_exceptions=True)
            if not stderr_task.done():
                await asyncio.gather(stderr_task, return_exceptions=True)
            self._active_process = None

    async def _drain_stream(self, stream: Any) -> tuple[bytes, bool]:
        """Drain a pipe concurrently while retaining only the first 32 KiB."""
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            remaining = self.HERMES_OUTPUT_LIMIT_BYTES - total
            if remaining > 0:
                chunks.append(chunk[:remaining])
            total += len(chunk)
        return b"".join(chunks), total > self.HERMES_OUTPUT_LIMIT_BYTES

    async def _terminate_process(self, process: Any) -> None:
        """Terminate the whole process group, escalating from TERM to KILL."""
        if getattr(process, "returncode", None) is not None:
            return
        self._signal_process_group(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=HERMES_TERM_GRACE_SECONDS)
            return
        except asyncio.TimeoutError:
            self._signal_process_group(process, signal.SIGKILL)
            await process.wait()

    @staticmethod
    def _signal_process_group(process: Any, signum: int) -> None:
        """Send a signal to the child process group, with a portable fallback."""
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
    "HERMES_TOOLSETS",
    "HISTORY_MAX_BYTES",
    "HISTORY_MAX_MESSAGES",
    "WORKING_UPDATE_INITIAL_DELAY_SECONDS",
    "WORKING_UPDATE_INTERVAL_SECONDS",
    "WORKING_UPDATE_MESSAGE",
    "HermesCancelled",
    "HermesError",
    "HermesHandler",
    "HermesInvocationError",
    "HermesMissingBinary",
    "HermesTimeout",
    "SAFETY_PROMPT",
    "is_cancel_phrase",
]
