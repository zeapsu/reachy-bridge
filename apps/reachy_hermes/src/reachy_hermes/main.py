"""Reachy Mini app entrypoint for the local Hermes voice assistant."""

from __future__ import annotations

import logging
import os
import threading
import time
from importlib import resources
from pathlib import Path
from typing import Any

from reachy_mini import ReachyMini, ReachyMiniApp
from reachy_mini.utils import create_head_pose
from reachy_mini.utils.interpolation import InterpolationTechnique

from reachy_chat.console import LocalStream
from reachy_chat.moves import MovementManager
from reachy_chat.tools.core_tools import ToolDependencies

from reachy_hermes.hermes import HermesHandler


logger = logging.getLogger(__name__)


class HermesLocalStream(LocalStream):
    """LocalStream variant that does not gate startup on an OpenRouter key."""

    def _has_required_key(self, backend: str) -> bool:
        """Hermes has its own local executable rather than a cloud credential."""
        del backend
        return True


class ReachyHermes(ReachyMiniApp):  # type: ignore[misc]
    """Reachy Mini app using local media, local Whisper/TTS, and Hermes."""

    custom_app_url: str | None = None
    request_media_backend = "local"

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        """Start speech and movement services, then cleanly return the robot home."""
        movement_manager = MovementManager(current_robot=reachy_mini, camera_worker=None)
        deps = ToolDependencies(
            reachy_mini=reachy_mini,
            movement_manager=movement_manager,
            camera_worker=None,
            vision_processor=None,
        )
        handler = HermesHandler(deps)
        stream = HermesLocalStream(handler, reachy_mini)

        def stop_stream() -> None:
            stop_event.wait()
            try:
                stream.close()
            except Exception:
                logger.exception("Hermes stream close failed")

        movement_manager.start()
        try:
            reachy_mini.enable_wobbling()
            threading.Thread(target=stop_stream, daemon=True, name="hermes-stop-watcher").start()
            try:
                # Readiness is intentionally delayed until the first Whisper model load finishes.
                handler.ensure_whisper_loaded()
                logger.info("Hermes Whisper model pre-warmed")
            except Exception as exc:
                logger.warning("Hermes Whisper pre-warm failed; startup will retry lazily: %s", exc)
            # a stop during model pre-warm must not launch or advertise a closed stream.
            if stop_event.is_set():
                logger.info("Hermes stopped during Whisper pre-warm")
                return
            _signal_ready()
            stream.launch()
        finally:
            try:
                stream.close()
            except Exception:
                logger.debug("Hermes stream was already closed", exc_info=True)
            try:
                movement_manager.stop()
            except Exception:
                logger.debug("Hermes movement manager stop failed", exc_info=True)
            try:
                reachy_mini.disable_wobbling()
            except Exception:
                logger.debug("Hermes wobbling shutdown failed", exc_info=True)
            _return_home(reachy_mini)
            try:
                reachy_mini.media.close()
            except Exception:
                logger.debug("Hermes media close failed", exc_info=True)
            try:
                reachy_mini.client.disconnect()
            except Exception:
                logger.debug("Hermes robot disconnect failed", exc_info=True)


def _signal_ready() -> None:
    """Touch the controller-provided readiness marker, when configured."""
    ready_file = os.getenv("REACHY_READY_FILE")
    if not ready_file:
        return
    try:
        Path(ready_file).touch()
        logger.debug("Signalled Hermes readiness via %s", ready_file)
    except Exception as exc:
        logger.debug("Could not write Hermes readiness file: %s", exc)


def _return_home(robot: ReachyMini) -> None:
    """Return head, antennas, and body yaw home, snapping only as a fallback."""
    home_target: dict[str, Any] = {
        "head": create_head_pose(degrees=True),
        "antennas": [-0.1745, 0.1745],
        "body_yaw": 0.0,
    }
    try:
        robot.goto_target(
            **home_target,
            duration=2.4,
            method=InterpolationTechnique.MIN_JERK,
        )
    except Exception as exc:
        logger.warning("hermes: graceful return-home unavailable; snapping home: %s", exc)
        try:
            robot.set_target(**home_target)
        except Exception as set_exc:
            logger.warning("hermes: failed to snap home on exit: %s", set_exc)
        return

    # The arrival cue is optional; motion cleanup must not depend on package assets.
    try:
        asset = resources.files("reachy_chat").joinpath("assets", "return_home.wav")
        with resources.as_file(asset) as sound_path:
            robot.media.play_sound(str(sound_path))
            time.sleep(0.5)
    except Exception as exc:
        logger.debug("hermes: return-home arrival cue unavailable: %s", exc)


def main() -> None:
    """Launch the Reachy Mini app through the SDK wrapper."""
    app = ReachyHermes()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        app.stop()


if __name__ == "__main__":
    main()

__all__ = ["HermesLocalStream", "ReachyHermes", "_return_home", "main"]
