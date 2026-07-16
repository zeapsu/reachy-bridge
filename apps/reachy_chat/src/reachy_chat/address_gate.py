"""Reusable addressed-to-me gating for speech input."""

from __future__ import annotations

import time
from dataclasses import dataclass
from math import isfinite, pi
from typing import Any


@dataclass(frozen=True)
class AddressResult:
    """Result of an address gate check."""

    is_open: bool
    confidence: float
    reason: str


class AddressGate:
    """Filter speech using direction, recent face detection, and wake words.

    Signal values can be injected into each check for deterministic tests. When
    omitted, the gate attempts best-effort reads from the robot and camera
    worker supplied at construction time. Missing or invalid signals never
    block speech: the gate degrades open to preserve existing behavior.
    """

    def __init__(
        self,
        reachy_mini: Any,
        camera_worker: Any | None = None,
        front_cone_half_width: float = 0.6,
        face_seen_timeout_s: float = 2.0,
        wake_words: tuple[str, ...] = ("reachy", "hey reachy"),
    ) -> None:
        """Configure signal sources and address-cue thresholds."""
        self.reachy_mini = reachy_mini
        self.camera_worker = camera_worker
        self.front_cone_half_width = front_cone_half_width
        self.face_seen_timeout_s = face_seen_timeout_s

        normalized_wake_words: list[str] = []
        for wake_word in wake_words:
            try:
                normalized = wake_word.lower().strip()
            except Exception:
                continue
            if normalized:
                normalized_wake_words.append(normalized)
        self.wake_words = tuple(normalized_wake_words)

    def fast_check(
        self,
        doa_result: tuple[float, bool] | None = None,
        face_seen_recently: bool | None = None,
        current_time: float | None = None,
    ) -> AddressResult:
        """Check DoA and recent face visibility without waiting for a transcript.

        Args:
            doa_result: ``(angle_radians, speech_detected)`` from ``get_DoA``.
            face_seen_recently: Whether a face was seen inside the timeout.
            current_time: Timestamp used for a fallback camera-worker read.

        Returns:
            An immutable decision with confidence and a human-readable reason.
        """
        try:
            resolved_doa = doa_result if doa_result is not None else self._read_doa()
            doa_available, front_facing = self._evaluate_doa(resolved_doa)
            face_available, face_detected = self._evaluate_face(
                face_seen_recently,
                current_time,
            )

            if front_facing and face_detected:
                return AddressResult(True, 1.0, "front_facing_and_face_detected")
            if front_facing:
                return AddressResult(True, 0.5, "front_facing")
            if face_detected:
                return AddressResult(True, 0.5, "face_detected")

            # Unknown inputs must not make simulation or partial hardware fail closed.
            if not doa_available or not face_available:
                return AddressResult(True, 0.0, "degrade_open")
            return AddressResult(False, 0.0, "no_address_cues")
        except Exception:
            # This gate sits on latency-sensitive audio paths and must never fail hard.
            return AddressResult(True, 0.0, "degrade_open")

    def semantic_check(
        self,
        transcript: str,
        doa_result: tuple[float, bool] | None = None,
        face_seen_recently: bool | None = None,
    ) -> AddressResult:
        """Add a wake-word override to the fast physical-signal check.

        Keyword matching is intentionally small and deterministic; this method
        is the extension point for a future semantic or model-based classifier.
        """
        try:
            normalized_transcript = transcript.lower()
            if any(wake_word in normalized_transcript for wake_word in self.wake_words):
                return AddressResult(True, 1.0, "wake_word_match")
        except Exception:
            # An unusable transcript still gets the fail-open physical check.
            pass

        result = self.fast_check(
            doa_result=doa_result,
            face_seen_recently=face_seen_recently,
        )
        if result.is_open:
            return result
        return AddressResult(False, 0.0, "no_address_cues")

    def _read_doa(self) -> tuple[float, bool] | None:
        """Read DoA from the robot without leaking hardware errors."""
        try:
            if self.reachy_mini is None:
                return None
            return self.reachy_mini.media.get_DoA()
        except Exception:
            return None

    def _evaluate_doa(self, doa_result: tuple[float, bool] | None) -> tuple[bool, bool]:
        """Return ``(available, front_facing)`` for a DoA value."""
        if doa_result is None:
            return False, False
        try:
            angle, speech_detected = doa_result
            # A reading is only usable as a direction signal when speech was
            # actually detected and the angle is a finite number; otherwise it
            # carries no information and must not be used to close the gate.
            if not bool(speech_detected):
                return False, False
            angle = float(angle)
            if not isfinite(angle):
                return False, False
            # Small epsilon keeps the cone boundary inclusive despite float error.
            is_front = abs(angle - pi / 2) <= self.front_cone_half_width + 1e-9
            return True, is_front
        except Exception:
            return False, False

    def _evaluate_face(
        self,
        face_seen_recently: bool | None,
        current_time: float | None,
    ) -> tuple[bool, bool]:
        """Return ``(available, recently_detected)`` for face state."""
        if face_seen_recently is not None:
            try:
                return True, bool(face_seen_recently)
            except Exception:
                return False, False

        if self.camera_worker is None:
            return False, False

        try:
            last_seen = self.camera_worker.last_face_detected_time
            if last_seen is None:
                return True, False
            now = time.time() if current_time is None else current_time
            elapsed = float(now) - float(last_seen)
            return True, 0.0 <= elapsed <= self.face_seen_timeout_s
        except Exception:
            return False, False
