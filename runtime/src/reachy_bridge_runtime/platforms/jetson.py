"""NVIDIA Jetson on JetPack 7: the tested platform.

JetPack 7 ships GStreamer 1.24 with the stock ``unixfdsink``/``unixfdsrc``,
and webrtcsink comes from gst-plugins-rs 0.14 (``tools/build_webrtcsink_jetson.sh``),
so the daemon's own media pipeline runs unchanged. What remains is host
setup for the Lite's USB audio, plus the media extras.
"""

from __future__ import annotations

import logging

from reachy_bridge_runtime import upstream
from reachy_bridge_runtime.platforms import Platform

logger = logging.getLogger(__name__)


def matches(model: str) -> bool:
    return "Tegra" in model or "NVIDIA" in model


def find_reachy_audio_card() -> int | None:
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


def set_speaker_volume() -> None:
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
    import subprocess

    try:
        card = find_reachy_audio_card()
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


def ensure_asoundrc() -> None:
    """Provision ``~/.asoundrc`` so audio bypasses PipeWire's flaky USB-audio
    profile picker, which sometimes only exposes a sink for the Reachy Mini
    Audio card and starves mic capture (#65). Upstream does this only for the
    Wireless robot; the Lite needs it too.
    """
    try:
        if upstream.ensure_reachymini_asoundrc():
            logger.info("Wrote ~/.asoundrc for Reachy Mini Audio (ALSA-direct, bypassing PipeWire).")
        else:
            logger.info("~/.asoundrc already configured for Reachy Mini Audio.")
    except Exception as e:
        logger.warning("Could not provision ~/.asoundrc for Reachy Mini Audio: %s", e)


PLATFORM = Platform(
    name="jetson",
    tier="tested",
    # Volume first: the asoundrc write doesn't touch the mixer.
    host_setup=(set_speaker_volume, ensure_asoundrc),
    features=frozenset({"audio_levels", "camera"}),
)
