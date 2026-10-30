"""Hear what Reachy says (#219): a copy of the robot's speaker output.

Everything the robot plays (its own sounds, an app's voice) goes to the
``reachymini_audio_sink`` ALSA device that upstream's ``~/.asoundrc``
defines. With an ALSA loopback card (``snd-aloop``, id ``Loopback``) this
module rewrites that device to play on both the speaker and the loopback,
so the audio gateway can capture the loopback's other end and send it to the
browser. The speaker branch has a mute switch on the loopback card, so the
robot goes quiet while a browser hears it instead:

    reachymini_audio_sink -> speaker (mute switch) + loopback
    loopback -> reachy_bridge_hear -> audio gateway -> browser
    browser -> audio gateway -> reachy_bridge_talk -> speaker (no mute, no loopback)

Nothing here is board-specific: any board whose ``~/.asoundrc`` has upstream's
devices gets it once the loopback card exists, and loses it again when the
card is gone. The upstream device names stay, so upstream's own check still
accepts the file.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

MARKER = "# reachy-bridge: speaker tee (#219)"
LOOPBACK = "Loopback"
SPEAKER_SWITCH = "Reachy Bridge Speaker"
HEAR_SOURCE = "reachy_bridge_hear"
TALK_SINK = "reachy_bridge_talk"
SINK = "reachymini_audio_sink"
# The speaker's original dmix, renamed: same ipc_key and hardware, so it
# shares the device with whatever already plays on it.
SPEAKER = "reachy_bridge_speaker_hw"

# The loopback runs at the speaker's format (upstream's dmix: 16 kHz, 2 ch).
_TEE = f"""
{MARKER}
pcm.reachy_bridge_speaker {{
    type softvol
    slave.pcm "{SPEAKER}"
    control {{ name "{SPEAKER_SWITCH}" card {LOOPBACK} }}
    resolution 2
}}

pcm.reachy_bridge_loop_out {{
    type dmix
    ipc_key 4251
    slave {{ pcm "hw:{LOOPBACK},0,0" channels 2 period_size 256 buffer_size 1024 rate 16000 format S16_LE }}
    bindings {{ 0 0 1 1 }}
}}

pcm.reachy_bridge_both {{
    type multi
    slaves.a {{ pcm "reachy_bridge_speaker" channels 2 }}
    slaves.b {{ pcm "reachy_bridge_loop_out" channels 2 }}
    bindings {{ 0 {{ slave a channel 0 }} 1 {{ slave a channel 1 }} 2 {{ slave b channel 0 }} 3 {{ slave b channel 1 }} }}
}}

pcm.{SINK} {{
    type plug
    slave.pcm {{
        type route
        slave {{ pcm "reachy_bridge_both" channels 4 }}
        ttable {{ 0.0 1 1.1 1 0.2 1 1.3 1 }}
    }}
}}

pcm.{HEAR_SOURCE} {{
    type dsnoop
    ipc_key 4252
    slave {{ pcm "hw:{LOOPBACK},1,0" channels 2 period_size 256 buffer_size 1024 rate 16000 format S16_LE }}
}}

pcm.{TALK_SINK} {{
    type plug
    slave.pcm "{SPEAKER}"
}}
"""

ASOUNDRC = Path.home() / ".asoundrc"
CARDS = Path("/proc/asound/cards")


def loopback_present(cards: str) -> bool:
    """``/proc/asound/cards`` lists a card with the id ``Loopback``."""
    return re.search(rf"^\s*\d+\s+\[{LOOPBACK}\s*\]", cards, re.MULTILINE) is not None


def installed(text: str) -> bool:
    return MARKER in text


def _block(text: str, name: str) -> tuple[int, int] | None:
    """Where ``pcm.<name> { ... }`` starts and ends in ``text``."""
    start = re.search(rf"^pcm\.{re.escape(name)}\s*\{{", text, re.MULTILINE)
    if start is None:
        return None
    depth = 0
    for i in range(start.end() - 1, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return start.start(), i + 1
    return None


def tee(text: str) -> str | None:
    """``text`` with the speaker tee, or None if it has no upstream sink."""
    if installed(text):
        return text
    span = _block(text, SINK)
    if span is None:
        return None
    start, end = span
    renamed = text[start:end].replace(f"pcm.{SINK}", f"pcm.{SPEAKER}", 1)
    return text[:start] + renamed + text[end:].rstrip("\n") + "\n" + _TEE


def untee(text: str) -> str:
    """``text`` as upstream wrote it."""
    if not installed(text):
        return text
    plain = text[: text.index(MARKER)].rstrip("\n") + "\n"
    return re.sub(rf"^pcm\.{SPEAKER}(\s*\{{)", rf"pcm.{SINK}\1", plain, count=1, flags=re.MULTILINE)


def is_installed() -> bool:
    try:
        return installed(ASOUNDRC.read_text(errors="ignore"))
    except OSError:
        return False


def set_speaker(on: bool) -> bool:
    """Unmute (or mute) the robot's speaker for its own sounds; False if the
    switch isn't there (it appears once something has played)."""
    try:
        result = subprocess.run(
            ["amixer", "-q", "-c", LOOPBACK, "cset", f"name={SPEAKER_SWITCH}", "on" if on else "off"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.warning("Couldn't switch the robot's speaker %s: %s", "on" if on else "off", error)
        return False
    if result.returncode != 0:
        logger.debug("Speaker switch not set: %s", (result.stderr or result.stdout).strip())
        return False
    return True


def ensure() -> None:
    """Host setup: add the tee when there's a loopback card, take it out when
    there isn't, and leave the speaker unmuted. Runs after upstream's
    ``~/.asoundrc`` is written."""
    try:
        text = ASOUNDRC.read_text(errors="ignore")
    except OSError:
        return
    try:
        present = loopback_present(CARDS.read_text())
    except OSError:
        present = False
    if present and not installed(text):
        new = tee(text)
        if new is None:
            return
        ASOUNDRC.write_text(new)
        logger.info("Added the speaker tee to ~/.asoundrc (the dashboard can hear what Reachy says).")
    elif not present and installed(text):
        ASOUNDRC.write_text(untee(text))
        logger.info("No ALSA loopback card; took the speaker tee out of ~/.asoundrc.")
    if present:
        set_speaker(True)
