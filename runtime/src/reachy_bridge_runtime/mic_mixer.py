"""The always-on mic mixer (#219): the real mic plus a browser's voice.

Every app reads ``reachymini_audio_src``. A mic device that joined the USB mic
and the loopback directly in ALSA made apps lose pieces of their own playback,
because the app's one GStreamer pipeline ran on that device's clock while the
two sides have separate clocks. Here a pipeline of its own takes that drift,
and apps read a single device on the loopback's clock (see ``audio_tee``).

The mic depends on this running, so systemd restarts it, and it waits for the
tee rather than failing while the daemon hasn't written it yet. Without a tee,
apps read upstream's mic directly and this just waits.
"""

from __future__ import annotations

import logging
import os
import shutil
import time

from reachy_bridge_runtime import audio_tee

logger = logging.getLogger(__name__)

_CAPS = "audio/x-raw,rate=16000,channels=2,format=S16LE"
_WAIT_S = 5.0


def command() -> list[str]:
    launch = shutil.which("gst-launch-1.0") or "gst-launch-1.0"
    pipeline = (
        f"audiomixer name=m ! {_CAPS} ! alsasink device={audio_tee.MIC_OUT} "
        f"alsasrc device={audio_tee.MIC} ! {_CAPS} ! queue ! m. "
        f"alsasrc device={audio_tee.APP_VOICE_SOURCE} ! {_CAPS} ! queue ! m."
    )
    return [launch, *pipeline.split()]


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    while not audio_tee.is_installed():
        time.sleep(_WAIT_S)
    logger.info("Mixing the mic and browser voice.")
    args = command()
    os.execv(args[0], args)
