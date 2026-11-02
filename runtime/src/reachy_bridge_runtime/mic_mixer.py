"""The always-on mic bridge (#219): the real mic onto the loopback.

Every app reads ``reachymini_audio_src``, which comes from the loopback. A mic
device that joined the USB mic and the loopback directly in ALSA made apps lose
pieces of their own playback, because the app's one GStreamer pipeline ran on
that device's clock while the two sides have separate clocks. Here alsaloop
crosses between the clocks, resampling to follow the difference, and a browser's
voice mixes with the mic in the loopback's dmix (see ``audio_tee``).

GStreamer couldn't do that crossing: every setting tried either skipped audio or
went silent, some only after 36 or 55 minutes. alsaloop ran clean for 80. It may
keep running when the USB mic goes away, though, so this stops it when the sound
cards change, and systemd starts a new one.

The mic depends on this running, so systemd restarts it, and it waits for the
tee rather than failing while the daemon hasn't written it yet. Without a tee,
apps read upstream's mic directly and this just waits.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time

from reachy_bridge_runtime import audio_tee

logger = logging.getLogger(__name__)

_WAIT_S = 5.0
_WATCH_S = 2.0


def command() -> list[str]:
    alsaloop = shutil.which("alsaloop") or "/usr/bin/alsaloop"
    # -S 4 -A 2: follow the clock difference with libsamplerate's fastest sinc.
    return [
        alsaloop,
        *f"-C {audio_tee.MIC} -P {audio_tee.MIC_OUT} -c 2 -r 16000 -f S16_LE -t 60000 -S 4 -A 2".split(),
    ]


def _cards() -> str:
    try:
        return audio_tee.CARDS.read_text(errors="replace")
    except OSError:
        return ""


def _bridge() -> int:
    """Run alsaloop until it exits or the sound cards change."""
    cards = _cards()
    process = subprocess.Popen(command())
    while process.poll() is None:
        time.sleep(_WATCH_S)
        if _cards() != cards:
            logger.warning("The sound cards changed; restarting the mic bridge.")
            process.terminate()
            process.wait()
            return 1
    return process.returncode


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if sys.argv[1:] == ["--untee"]:
        audio_tee.remove()
        return
    if sys.argv[1:] == ["--tee"]:
        audio_tee.ensure()
        return
    while not audio_tee.is_installed():
        time.sleep(_WAIT_S)
    logger.info("Bridging the mic onto the loopback.")
    sys.exit(_bridge())
