"""Reachy Mini app entrypoint for the local Hermes voice assistant."""
# ruff: noqa: E402

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


def _signal_stage(stage: str) -> None:
    """Best-effort publish the current startup stage for the controller."""
    ready_file = os.getenv("REACHY_READY_FILE")
    if not ready_file:
        return
    try:
        Path(f"{ready_file}.stage").write_text(stage, encoding="utf-8")
    except Exception as exc:
        logger.warning("startup stage write failed: %s", exc)


_signal_stage("importing")

from reachy_mini import ReachyMini, ReachyMiniApp
from reachy_mini.utils import create_head_pose
from reachy_mini.utils.interpolation import InterpolationTechnique

from reachy_chat.console import LocalStream
from reachy_chat.moves import MovementManager
from reachy_chat.tools.core_tools import ToolDependencies
from reachy_chat.utils import setup_logger

from reachy_hermes.gateway import (
    HermesGatewayClient,
    HermesInvocationError,
    HermesMissingBinary,
    HermesRuntimeMismatch,
)
from reachy_hermes.hermes import HermesHandler


@contextmanager
def _skip_local_camera_initialization() -> Iterator[None]:
    """Keep Hermes' local audio backend from opening an unused camera reader."""
    from reachy_mini.media.media_manager import MediaManager

    original_init_camera = MediaManager._init_camera

    def skip_camera_init(self: Any, *args: Any, **kwargs: Any) -> None:
        del self, args, kwargs
        logger.info("Hermes is audio-only; skipping local camera IPC initialization")

    MediaManager._init_camera = skip_camera_init  # type: ignore[method-assign]
    try:
        yield
    finally:
        MediaManager._init_camera = original_init_camera  # type: ignore[method-assign]


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

    def wrapped_run(self) -> None:
        """Run Hermes with local audio only, leaving the daemon camera shared."""
        with _skip_local_camera_initialization():
            super().wrapped_run()


    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        """Start speech and movement services, then cleanly return the robot home."""
        movement_manager = MovementManager(current_robot=reachy_mini, camera_worker=None)
        deps = ToolDependencies(
            reachy_mini=reachy_mini,
            movement_manager=movement_manager,
            camera_worker=None,
            vision_processor=None,
        )
        gateway = HermesGatewayClient(stage_callback=_signal_stage)
        # Verify synchronously so a mismatched runtime is rejected at startup (#103);
        # that is the only fatal case. A missing or unrunnable binary must stay
        # fail-open exactly as before the pin: log and continue so the app still
        # serves audio and speaks the error (hermes.py's handlers). verify_runtime
        # caches identity only on success, so the lazy verify in gateway.prepare()
        # re-raises these into those handlers.
        try:
            asyncio.run(gateway.verify_runtime())
        except HermesRuntimeMismatch:
            raise
        except (HermesMissingBinary, HermesInvocationError) as exc:
            logger.warning(
                "Hermes runtime verification deferred; voice requests will explain "
                "the error: %s",
                exc,
            )
        handler = HermesHandler(deps, gateway=gateway)
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
                # Readiness waits for both local speech models so the first turn
                # does not pay a hidden download or initialization delay.
                handler.ensure_whisper_loaded()
                logger.info("Hermes Whisper model pre-warmed")
            except Exception as exc:
                logger.warning("Hermes Whisper pre-warm failed; startup will retry lazily: %s", exc)
            if stop_event.is_set():
                logger.info("Hermes stopped during Whisper pre-warm")
                return
            try:
                handler.ensure_tts_loaded()
                logger.info("Hermes neural TTS model pre-warmed")
            except Exception as exc:
                logger.warning("Hermes neural TTS pre-warm failed; startup will retry lazily: %s", exc)
            if stop_event.is_set():
                logger.info("Hermes stopped during speech-model pre-warm")
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
    _signal_stage("ready")
    ready_file = os.getenv("REACHY_READY_FILE")
    if not ready_file:
        return
    try:
        Path(ready_file).touch()
        logger.debug("Signalled Hermes readiness via %s", ready_file)
    except Exception as exc:
        logger.warning(
            "readiness marker write failed: %s — the controller will stop this "
            "app when the startup budget expires",
            exc,
        )


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
    setup_logger(False)
    logger.info("Starting Reachy Hermes voice app")
    _signal_stage("connecting-daemon")
    app = ReachyHermes()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        app.stop()


if __name__ == "__main__":
    main()

__all__ = ["HermesLocalStream", "ReachyHermes", "_return_home", "main"]
