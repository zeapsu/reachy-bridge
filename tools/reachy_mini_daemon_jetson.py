"""Launch the Reachy Mini daemon (``reachy-mini`` 1.12.0) with bridge patches.

All CLI args are forwarded to the stock daemon entry point. Patches are applied
at runtime and never edit the venv, so they survive ``uv sync`` and SDK
upgrades. Each one was checked against the 1.12.0 source:

- ``_apply_keep_awake_on_app_stop_patch`` (all platforms): a requested app stop
  leaves the robot awake at the home pose instead of triggering the daemon's
  idle-reset sleep. Bridge policy, not a platform fix; see
  ``docs/daemon-contract.md``.
- ``_set_reachy_audio_output_volume`` (Jetson): the Lite speaker's ALSA
  control ``PCM,1`` can start attenuated, which makes the daemon's sounds
  inaudible.
- ``_ensure_reachymini_asoundrc`` (Jetson): upstream writes ``~/.asoundrc``
  only for the Wireless robot; the Lite needs it too (#65).
- ``reachy_mini_daemon_media`` (Jetson): the audio spectrum tap and the
  ``/api/camera/stream.mjpg`` and audio-level routes the controller uses.

Removed in #146: the ``unixfd`` -> ``nvunixfd`` element alias, the NVMM camera
IPC producer, and the gst-plugins-rs 0.10 ``run-signalling-server`` shim.
JetPack 7 ships GStreamer 1.24 with the stock ``unixfdsink``/``unixfdsrc``,
and webrtcsink 0.14 has ``run-signalling-server``, so upstream's own pipeline
works unchanged. The NVMM producer also stopped stock community apps, which
read the camera with ``unixfdsrc``, from receiving frames.

Usage (from the repo root, uses the root venv that has reachy-mini installed):

    uv run python tools/reachy_mini_daemon_jetson.py            # Lite over USB
    uv run python tools/reachy_mini_daemon_jetson.py --sim --headless
"""

import logging
import platform
import sys

logger = logging.getLogger("reachy_mini_daemon_jetson")


def _is_jetson() -> bool:
    if platform.system() != "Linux":
        return False
    try:
        with open("/proc/device-tree/model", "r") as f:
            model = f.read()
    except OSError:
        return False
    return "Tegra" in model or "NVIDIA" in model


def _apply_keep_awake_on_app_stop_patch() -> None:
    """Keep the robot awake when the operator stops an app.

    Upstream, every app exit frees the robot slot, which schedules an idle
    reset: 1.5 s later the robot plays the sleep move and goes limp. That is
    right for a crash, but a requested stop should leave the robot at the
    home pose the app manager already returns it to. So the became-free hook
    is detached for the duration of ``stop_current_app`` only; crashes and
    remote-session drops still trigger the reset.
    """
    try:
        from reachy_mini.apps.manager import AppManager
    except ImportError as e:
        logger.warning("Could not import AppManager for keep-awake patch: %s", e)
        return

    original = AppManager.stop_current_app

    async def stop_current_app(self, *args, **kwargs):
        daemon = getattr(self, "daemon", None)
        lock = getattr(daemon, "robot_app_lock", None)
        handler = getattr(lock, "_on_became_free", None)
        if lock is None or handler is None:
            return await original(self, *args, **kwargs)
        lock.set_on_became_free_handler(None)
        try:
            return await original(self, *args, **kwargs)
        finally:
            backend = getattr(daemon, "backend", None)
            # daemon.stop() detaches the hook itself; don't re-arm it then.
            if (
                lock._on_became_free is None
                and backend is not None
                and not getattr(backend, "is_shutting_down", False)
            ):
                lock.set_on_became_free_handler(handler)

    AppManager.stop_current_app = stop_current_app
    logger.info("Patched AppManager.stop_current_app to keep the robot awake on stop")


def _find_reachy_audio_card() -> int | None:
    """Return the ALSA card number for the Reachy Mini Audio device."""
    try:
        with open("/proc/asound/cards", "r") as f:
            lines = f.readlines()
    except OSError:
        return None

    card_number = None
    card_text = []
    for line in lines:
        stripped = line.lstrip()
        number, separator, remainder = stripped.partition(" ")
        if separator and number.isdigit() and remainder.lstrip().startswith("["):
            if card_number is not None:
                description = " ".join(card_text).casefold()
                if "reachy mini audio" in description or "pollen" in description:
                    return card_number
            card_number = int(number)
            card_text = [remainder]
        elif card_number is not None:
            card_text.append(line)

    if card_number is not None:
        description = " ".join(card_text).casefold()
        if "reachy mini audio" in description or "pollen" in description:
            return card_number
    return None


def _set_reachy_audio_output_volume() -> None:
    """Discover the Reachy Mini USB audio card by name and set full volume.

    The Reachy Mini Audio USB device (alsa_output.usb-Pollen_Robotics_Reachy_Mini_Audio_...)
    has two PCM mixer controls:
    - PCM,0 (stereo, typically unused)
    - PCM,1 (mono playback to the speaker)

    The speaker is routed through PCM,1. If this control is below 100% (hardware-level
    attenuation, typically -20dB at 67%), the daemon's sound playback (wake-up, sleep,
    etc.) is inaudible. This patch ensures full volume at startup.

    This is a one-time ALSA mixer adjustment, not a PulseAudio control — it persists
    across daemon restarts and PulseAudio reconnects.
    """
    if not _is_jetson():
        return

    import subprocess

    try:
        card = _find_reachy_audio_card()
        if card is None:
            logger.warning("Reachy Mini Audio ALSA card not found; speaker volume not adjusted")
            return

        # Set PCM,1 (mono playback) to 100% on the Reachy Mini Audio card
        result = subprocess.run(
            ["amixer", "-c", str(card), "sset", "PCM,1", "100%"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            logger.info(
                "Set Reachy Mini Audio ALSA card %d PCM,1 (speaker) to 100%% volume", card
            )
        else:
            logger.warning(
                "amixer failed to set Reachy Audio volume: %s", result.stderr or result.stdout
            )
    except FileNotFoundError:
        logger.warning("amixer not found; Reachy Audio speaker volume may be low")
    except subprocess.TimeoutExpired:
        logger.warning("amixer timeout while setting Reachy Audio volume")
    except Exception as e:
        logger.warning("Error setting Reachy Audio volume: %s", e)


def _ensure_reachymini_asoundrc() -> None:
    """Provision ~/.asoundrc so audio bypasses PipeWire's flaky USB-audio
    profile picker (PipeWire sometimes only exposes a Sink for the "Reachy
    Mini Audio" card, silently starving mic capture — #65). Upstream only
    does this for --wireless-version; Lite hardware needs it here too.
    """
    if not _is_jetson():
        return
    try:
        from reachy_mini.media.audio_utils import (
            check_reachymini_asoundrc,
            write_asoundrc_to_home,
        )

        if check_reachymini_asoundrc():
            logger.info("~/.asoundrc already correctly configured for Reachy Mini Audio.")
            return
        write_asoundrc_to_home()
        logger.info("Wrote ~/.asoundrc for Reachy Mini Audio (ALSA-direct, bypassing PipeWire).")
    except Exception as e:
        logger.warning("Could not provision ~/.asoundrc for Reachy Mini Audio: %s", e)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    _apply_keep_awake_on_app_stop_patch()
    if _is_jetson():
        # Importing the extension is lazy so this wrapper remains a transparent
        # daemon pass-through on non-Jetson development machines.
        from reachy_mini_daemon_media import (
            apply_jetson_audio_spectrum_patch,
            install_audio_levels_route,
            install_camera_stream_route,
        )

        # Patch the daemon-owned capture pipeline before GstMediaServer is
        # constructed, then install both lazy media routes on create_app.
        apply_jetson_audio_spectrum_patch()
        install_camera_stream_route()
        install_audio_levels_route()
    _set_reachy_audio_output_volume()
    _ensure_reachymini_asoundrc()
    from reachy_mini.daemon.app.main import main as daemon_main

    return daemon_main()


if __name__ == "__main__":
    sys.exit(main())
