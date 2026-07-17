"""Voice pipeline for the isolated Hermes gateway-backed Reachy app."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeAlias, cast

import numpy as np
from fastrtc import AdditionalOutputs
from reachy_mini.motion.move import Move
from numpy.typing import NDArray
from reachy_chat.openrouter_chat import OpenRouterChatHandler

from reachy_hermes.gateway import (
    HERMES_BINARY,
    HERMES_TOOLSETS,
    HermesCancelled,
    HermesError,
    HermesGatewayClient,
    HermesInvocationError,
    HermesMissingBinary,
    HermesTimeout,
    SAFETY_PROMPT,
)
from reachy_hermes.tts import NeuralTTS

logger = logging.getLogger(__name__)

WORKING_UPDATE_INITIAL_DELAY_SECONDS = 24.0
WORKING_UPDATE_INTERVAL_SECONDS = 32.0
PROCESSING_BURST_INTERVAL_SECONDS = 1.16
WORKING_UPDATE_MESSAGE = "I'm still working on that."
GATEWAY_HISTORY_MESSAGES = 6
CANCEL_REPLY = "Okay, I stopped."
MISSING_BINARY_REPLY = "I can't start Hermes because its executable is missing."
FAILURE_REPLY = "I couldn't complete that request because Hermes failed."
WHISPER_INITIAL_PROMPT = "Reachy Mini. Hermes."

_CANCEL_PATTERN = re.compile(r"\b(?:stop|cancel|never\s+mind)\b", re.IGNORECASE)
ProcessFactory: TypeAlias = Callable[..., Awaitable[Any]]


def is_cancel_phrase(transcript: str) -> bool:
    """Return whether speech contains one of the supported stop phrases."""
    normalized = re.sub(r"[^a-z]+", " ", transcript.casefold()).strip()
    return _CANCEL_PATTERN.search(normalized) is not None

def to_spoken_text(text: str) -> str:
    """Convert defensive leftovers from Markdown into speech-safe plain text."""
    spoken = re.sub(r"```(?:\w+)?\s*(.*?)```", r"\1", text, flags=re.DOTALL)
    spoken = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", spoken)
    spoken = re.sub(r"\[\d+\]", "", spoken)
    spoken = re.sub(r"https?://\S+", "", spoken)
    spoken = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", spoken)
    spoken = re.sub(r"(?m)^\s*[-*+]\s+", "", spoken)
    spoken = spoken.translate(str.maketrans({"*": "", "#": "", "`": "", "_": " ", "~": ""}))
    return re.sub(r"\s+", " ", spoken).strip()

def split_for_speech(text: str, maximum: int = 90) -> list[str]:
    """Split plain text at sentence boundaries for low-latency synthesis."""
    chunks: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        words = sentence.split()
        while words:
            candidate = f"{current} {' '.join(words)}".strip()
            if len(candidate) <= maximum:
                current = candidate
                break
            room = maximum - len(current) - (1 if current else 0)
            taken: list[str] = []
            while words and (not taken or len(" ".join(taken + [words[0]])) <= room):
                taken.append(words.pop(0))
            if current:
                chunks.append(current)
                current = ""
                words = taken + words
            else:
                chunks.append(" ".join(taken))
    if current:
        chunks.append(current)
    return chunks


class EarWiggleMove(Move):  # type: ignore[misc]
    """Short antenna-only acknowledgement routed through MovementManager."""

    def __init__(
        self,
        head: NDArray[np.float32],
        antennas: tuple[float, float],
        body_yaw: float,
    ) -> None:
        self.head = head.copy()
        self.antennas = antennas
        self.body_yaw = body_yaw

    @property
    def duration(self) -> float:
        return 0.66

    def evaluate(
        self, t: float
    ) -> tuple[NDArray[np.float32], NDArray[np.float64], float]:
        clamped = min(max(t, 0.0), self.duration)
        offset = np.deg2rad(4.0) * np.sin(np.pi * clamped / self.duration) ** 2
        antennas = np.array(
            [self.antennas[0] + offset, self.antennas[1] - offset],
            dtype=np.float64,
        )
        return self.head, antennas, self.body_yaw


class HermesHandler(OpenRouterChatHandler):
    """Reachy-owned STT/TTS with a persistent, isolated Hermes event loop."""

    SPEECH_ENERGY_THRESHOLD = 0.015
    SILENCE_DURATION_S = 0.6
    MIN_SPEECH_DURATION_S = 0.2
    PRE_ROLL_SECONDS = 0.35
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
        gateway: HermesGatewayClient | None = None,
        neural_tts: NeuralTTS | None = None,
    ) -> None:
        """Construct lazily; no gateway, speech model, or camera starts here."""
        super().__init__(deps, instance_path=instance_path, startup_voice=startup_voice)
        self.hermes_binary = Path(hermes_binary).expanduser()
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self.gateway = gateway or HermesGatewayClient(
            hermes_binary=self.hermes_binary,
            process_factory=self._process_factory,
        )
        self.neural_tts = neural_tts or NeuralTTS()
        self._working_status_task: asyncio.Task[None] | None = None
        self._gateway_turn_active = False
        self._cancel_detection_task: asyncio.Task[None] | None = None
        self._cancel_speech_active = False
        self._cancel_speech_chunks: list[NDArray[np.float32]] = []
        self._cancel_speech_samples = 0
        self._cancel_silence_samples = 0
        self._pre_roll_chunks: deque[NDArray[np.float32]] = deque()
        self._pre_roll_samples = 0
        self._output_playback_until = 0.0

    def copy(self) -> "HermesHandler":
        """Create a fresh handler while preserving the configured binary."""
        return HermesHandler(
            self.deps,
            self.instance_path,
            startup_voice=self._voice_override,
            hermes_binary=self.hermes_binary,
            process_factory=self._process_factory,
        )

    async def start_up(self) -> None:
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
        """Stop cancellation detection, gateway work, and local media resources."""
        self._stop_event.set()
        detection_task = self._cancel_detection_task
        if detection_task is not None and detection_task is not asyncio.current_task() and not detection_task.done():
            detection_task.cancel()
            await asyncio.gather(detection_task, return_exceptions=True)
        self._cancel_detection_task = None
        await self._stop_working_status_updates()
        await self.gateway.close()
        await super().shutdown()

    async def receive(self, frame: Any) -> None:
        """Detect speech with a short pre-roll and accept cancellation while working."""
        if self._stop_event.is_set():
            return
        if self._processing_turn:
            self._receive_cancel_frame(frame)
            return

        sample_rate, samples = frame
        audio = self._prepare_audio(samples, sample_rate)
        if audio.size == 0:
            return
        energy = float(np.sqrt(np.mean(np.square(audio, dtype=np.float32))))
        is_speech = energy >= self.SPEECH_ENERGY_THRESHOLD

        if not self._speech_active:
            if not is_speech:
                self._remember_pre_roll(audio)
                return
            self._speech_active = True
            self._set_listening(True)
            self._mark_activity("user_speech_started")
            logger.info("Hermes speech started at RMS %.4f", energy)
            self._speech_chunks.extend(self._pre_roll_chunks)
            self._clear_pre_roll()

        self._speech_chunks.append(audio)
        if is_speech:
            self._speech_samples += audio.size
            self._silence_samples = 0
        else:
            self._silence_samples += audio.size

        utterance_duration = sum(chunk.size for chunk in self._speech_chunks) / self.SAMPLE_RATE
        trailing_silence = self._silence_samples / self.SAMPLE_RATE
        should_finalize = trailing_silence >= self.SILENCE_DURATION_S or utterance_duration >= self.MAX_UTTERANCE_DURATION_S
        if not should_finalize:
            return

        speech_duration = self._speech_samples / self.SAMPLE_RATE
        utterance = np.concatenate(self._speech_chunks) if self._speech_chunks else np.empty(0, dtype=np.float32)
        logger.info(
            "Hermes utterance finalized after %.2fs with %.2fs speech and %.2fs trailing silence",
            utterance_duration,
            speech_duration,
            trailing_silence,
        )
        self._reset_input_buffer()
        self._clear_pre_roll()
        self._set_listening(False)
        self._mark_activity("user_speech_stopped")
        if speech_duration < self.MIN_SPEECH_DURATION_S:
            return
        self._processing_turn = True
        self._processing_task = asyncio.create_task(self._process_turn(utterance), name="hermes-voice-turn")

    async def _process_turn(self, audio: NDArray[np.float32]) -> None:
        """Run STT and Hermes, then synthesize sentence chunks ahead of playback."""
        started = time.monotonic()
        self._processing_turn = True
        try:
            try:
                transcript = (await self._transcribe(audio)).strip()
            except Exception as exc:
                logger.exception("Hermes speech transcription failed: %s", exc)
                await self.output_queue.put(
                    (self.SAMPLE_RATE, np.zeros(1_600, dtype=np.int16))
                )
                return
            if not transcript:
                logger.debug("Ignoring empty Hermes transcription")
                return

            address_result = self.address_gate.semantic_check(transcript)
            logger.info(
                "Hermes address gate: open=%s confidence=%.1f reason=%s",
                address_result.is_open,
                address_result.confidence,
                address_result.reason,
            )
            if not address_result.is_open:
                logger.info(
                    "Filtered overheard Hermes speech confidence=%.1f transcript=%r",
                    address_result.confidence,
                    transcript,
                )
                return

            self._record_message("user", transcript)
            await self.output_queue.put(
                AdditionalOutputs({"role": "user", "content": transcript})
            )
            self._mark_activity("user_transcription_completed")

            try:
                reply = await self._request_completion()
            except Exception as exc:
                logger.exception("Hermes completion failed: %s", exc)
                reply = self.FALLBACK_REPLY

            self._record_message("assistant", reply)
            await self.output_queue.put(
                AdditionalOutputs({"role": "assistant", "content": reply})
            )
            self._mark_activity("assistant_transcript_done")

            playback_started: float | None = None
            playback_seconds = 0.0
            for speech_text in split_for_speech(reply):
                try:
                    sample_rate, speech = await self._synthesize(speech_text)
                except Exception as exc:
                    logger.exception("Hermes text-to-speech synthesis failed: %s", exc)
                    break
                if playback_started is None:
                    playback_started = time.monotonic()
                    self._mark_activity("assistant_audio_delta")
                self._note_output_playback(sample_rate, speech.size)
                for offset in range(0, speech.size, self.OUTPUT_CHUNK_SAMPLES):
                    await self.output_queue.put(
                        (
                            sample_rate,
                            speech[offset : offset + self.OUTPUT_CHUNK_SAMPLES],
                        )
                    )
                playback_seconds += speech.size / sample_rate

            if playback_started is not None:
                elapsed = time.monotonic() - playback_started
                await asyncio.sleep(max(0.0, playback_seconds - elapsed))
            logger.info("Hermes voice turn completed in %.2fs", time.monotonic() - started)
        finally:
            self._processing_turn = False
            self._reset_input_buffer()
            self._clear_pre_roll()

    def _remember_pre_roll(self, audio: NDArray[np.float32]) -> None:
        self._pre_roll_chunks.append(audio)
        self._pre_roll_samples += audio.size
        maximum = int(self.PRE_ROLL_SECONDS * self.SAMPLE_RATE)
        while self._pre_roll_chunks and self._pre_roll_samples > maximum:
            removed = self._pre_roll_chunks.popleft()
            self._pre_roll_samples -= removed.size

    def _clear_pre_roll(self) -> None:
        self._pre_roll_chunks.clear()
        self._pre_roll_samples = 0

    def _receive_cancel_frame(self, frame: Any) -> None:
        if time.monotonic() < self._output_playback_until:
            if self._cancel_speech_active:
                self._reset_cancel_input()
                self._set_listening(False)
            return
        sample_rate, samples = frame
        audio = self._prepare_audio(samples, sample_rate)
        if audio.size == 0:
            return
        energy = float(np.sqrt(np.mean(np.square(audio, dtype=np.float32))))
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
        if not self._cancel_speech_active or (
            silence < self.SILENCE_DURATION_S and duration < self.MAX_UTTERANCE_DURATION_S
        ):
            return
        utterance = self._join_cancel_audio()
        self._reset_cancel_input()
        self._set_listening(False)
        if self._cancel_detection_task is None or self._cancel_detection_task.done():
            self._cancel_detection_task = asyncio.create_task(
                self._check_cancel_utterance(utterance), name="hermes-cancel-detection"
            )

    def _join_cancel_audio(self) -> NDArray[np.float32]:
        if not self._cancel_speech_chunks:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(self._cancel_speech_chunks)

    def _reset_cancel_input(self) -> None:
        self._cancel_speech_active = False
        self._cancel_speech_chunks.clear()
        self._cancel_speech_samples = 0
        self._cancel_silence_samples = 0

    async def _check_cancel_utterance(self, audio: NDArray[np.float32]) -> None:
        try:
            transcript = (await self._transcribe(audio)).strip()
        except Exception as exc:
            logger.debug("Hermes cancellation transcription failed: %s", exc)
            return
        if is_cancel_phrase(transcript):
            logger.info("Hermes request cancelled by voice phrase: %r", transcript)
            await self.cancel_current()

    def _note_output_playback(self, sample_rate: int, sample_count: int) -> None:
        duration = sample_count / sample_rate
        self._output_playback_until = max(
            time.monotonic(), self._output_playback_until
        ) + duration

    async def cancel_current(self) -> None:
        """Interrupt the active gateway session after a spoken cancellation request."""
        await self._stop_working_status_updates()
        await self.gateway.interrupt()
        clear_queue = self._clear_queue
        if clear_queue is not None:
            try:
                clear_queue()
            except Exception:
                logger.debug("Unable to clear audio after Hermes cancellation", exc_info=True)

    def ensure_tts_loaded(self) -> None:
        """Ensure the neural speech engine and verified model are ready."""
        self.neural_tts.ensure_loaded()

    def _transcribe_blocking(self, audio: NDArray[np.float32]) -> str:
        started = time.monotonic()
        self.ensure_whisper_loaded()
        segments, _ = self._whisper_model.transcribe(
            audio,
            beam_size=1,
            vad_filter=True,
            initial_prompt=WHISPER_INITIAL_PROMPT,
            condition_on_previous_text=False,
        )
        transcript = " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
        logger.info("Hermes STT completed in %.2fs: %r", time.monotonic() - started, transcript)
        return transcript

    def _synthesize_blocking(self, text: str) -> tuple[int, NDArray[np.int16]]:
        started = time.monotonic()
        result = self.neural_tts.synthesize(text)
        logger.info("Hermes TTS completed in %.2fs for %d characters", time.monotonic() - started, len(text))
        return result

    async def _request_completion(self, image_jpeg: bytes | None = None) -> str:
        """Submit the current text turn and convert gateway failures to speech."""
        del image_jpeg
        self._reset_cancel_input()
        self._set_listening(False)
        await self._queue_processing_chime()
        self._gateway_turn_active = True
        self._start_working_status_updates()
        try:
            try:
                answer = await self.invoke_hermes()
                spoken = to_spoken_text(answer)
                if not spoken:
                    raise HermesInvocationError("Hermes returned no speech-safe answer")
                return spoken
            except HermesCancelled:
                return CANCEL_REPLY
            except HermesMissingBinary as exc:
                logger.error("Hermes executable missing: %s", exc)
                return MISSING_BINARY_REPLY
            except HermesError as exc:
                logger.error("Hermes request failed: %s", exc)
                return FAILURE_REPLY
        finally:
            self._gateway_turn_active = False
            await self._stop_working_status_updates()
            self._reset_cancel_input()
            self._set_listening(False)

    async def invoke_hermes(self, prompt: str | None = None) -> str:
        """Send the current turn with bounded plain-text context."""
        if prompt is None:
            prompt = next(
                (
                    cast(str, message["content"])
                    for message in reversed(self.conversation_history)
                    if message["role"] == "user"
                ),
                "",
            )
        if not prompt.strip():
            raise HermesInvocationError("Hermes request has no user prompt")

        history_source = self.conversation_history
        if history_source and history_source[-1]["role"] == "user" and history_source[-1]["content"] == prompt:
            history_source = history_source[:-1]
        history: list[dict[str, str]] = []
        for message in history_source[-GATEWAY_HISTORY_MESSAGES:]:
            content = to_spoken_text(cast(str, message["content"]))
            if content and message["role"] in {"user", "assistant"}:
                history.append({"role": cast(str, message["role"]), "content": content})

        started = time.monotonic()
        answer = await self.gateway.ask(
            prompt.strip(),
            history=history,
            status_callback=self._on_gateway_status,
        )
        logger.info("Hermes agent turn completed in %.2fs", time.monotonic() - started)
        return answer

    def _on_gateway_status(self, status: str) -> None:
        logger.info("Hermes status: %s", status)
        self._mark_activity("hermes_working")

    async def _queue_processing_chime(self) -> None:
        """Start the repeating three-pulse processing earcon."""
        await self._queue_processing_burst()
        self._queue_ear_wiggle()
        self._mark_activity("hermes_processing_chime")

    async def _queue_progress_tick(self) -> None:
        """Repeat one subtle processing burst while Hermes is working."""
        await self._queue_processing_burst()
        self._queue_ear_wiggle()

    async def _queue_processing_burst(self) -> None:
        pulse_samples = int(self.SAMPLE_RATE * 0.16)
        onset_samples = int(self.SAMPLE_RATE * 0.25)
        burst = np.zeros(2 * onset_samples + pulse_samples, dtype=np.float32)
        times = np.arange(pulse_samples, dtype=np.float32) / self.SAMPLE_RATE

        attack_samples = int(self.SAMPLE_RATE * 0.01)
        sustain_samples = int(self.SAMPLE_RATE * 0.08)
        release_samples = pulse_samples - attack_samples - sustain_samples
        envelope = np.concatenate(
            (
                np.sin(np.linspace(0.0, np.pi / 2.0, attack_samples, endpoint=False)) ** 2,
                np.ones(sustain_samples, dtype=np.float64),
                np.cos(np.linspace(0.0, np.pi / 2.0, release_samples)) ** 2,
            )
        ).astype(np.float32)

        for index, frequency in enumerate((440.0, 494.0, 554.0)):
            amplitude = 0.09 * (0.78**index)
            wave = (
                np.sin(2.0 * np.pi * frequency * times)
                + 0.2 * np.sin(4.0 * np.pi * frequency * times)
            ) / 1.2
            offset = index * onset_samples
            burst[offset : offset + pulse_samples] = amplitude * envelope * wave

        self._note_output_playback(self.SAMPLE_RATE, burst.size)
        await self.output_queue.put(
            (
                self.SAMPLE_RATE,
                np.round(burst * 32767.0).astype(np.int16),
            )
        )

    def _queue_ear_wiggle(self) -> None:
        try:
            robot = self.deps.reachy_mini
            head = np.asarray(robot.get_current_head_pose(), dtype=np.float32)
            head_joints, antenna_joints = robot.get_current_joint_positions()
            body_yaw = float(head_joints[0])
            antennas = (float(antenna_joints[0]), float(antenna_joints[1]))
            self.deps.movement_manager.queue_move(
                EarWiggleMove(head, antennas, body_yaw)
            )
        except Exception:
            logger.debug("Unable to queue Hermes ear wiggle", exc_info=True)

    def _start_working_status_updates(self) -> None:
        task = self._working_status_task
        if task is not None and not task.done():
            return
        self._working_status_task = asyncio.create_task(
            self._working_status_updates(), name="hermes-working-status"
        )

    async def _stop_working_status_updates(self) -> None:
        task = self._working_status_task
        self._working_status_task = None
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _working_status_updates(self) -> None:
        elapsed = 0.0
        next_spoken_update = self.WORKING_UPDATE_INITIAL_DELAY_SECONDS
        try:
            while self._gateway_turn_active:
                await asyncio.sleep(PROCESSING_BURST_INTERVAL_SECONDS)
                if not self._gateway_turn_active:
                    return
                elapsed += PROCESSING_BURST_INTERVAL_SECONDS
                await self._queue_progress_tick()
                if elapsed >= next_spoken_update:
                    await self._speak_working_update()
                    next_spoken_update += self.WORKING_UPDATE_INTERVAL_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Hermes working-status speech failed")

    async def _speak_working_update(self) -> None:
        message = self.WORKING_UPDATE_MESSAGE
        await self.output_queue.put(AdditionalOutputs({"role": "assistant", "content": message}))
        try:
            sample_rate, speech = await self._synthesize(message)
        except Exception:
            logger.exception("Hermes working-status TTS failed")
            return
        self._note_output_playback(sample_rate, speech.size)
        for offset in range(0, speech.size, self.OUTPUT_CHUNK_SAMPLES):
            await self.output_queue.put((sample_rate, speech[offset : offset + self.OUTPUT_CHUNK_SAMPLES]))
        if speech.size:
            await asyncio.sleep(speech.size / sample_rate)


__all__ = [
    "HERMES_BINARY",
    "HERMES_TOOLSETS",
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
    "to_spoken_text",
]
