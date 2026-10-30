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

The speaker and the loopback open together (ALSA's ``multi``), so something
else holding the loopback would silence the robot: WirePlumber, which would
otherwise offer the card as a sound device, is told to leave it alone.
"""

from __future__ import annotations

import logging
import os
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
# WirePlumber 0.4 (Lua) and 0.5 (SPA-JSON) read different files; each ignores
# the other's.
_WIREPLUMBER = Path.home() / ".config/wireplumber"
WIREPLUMBER_RULES = {
    _WIREPLUMBER / "main.lua.d/51-reachy-bridge-loopback.lua": """-- reachy-bridge (#219): the loopback carries the robot's speaker; leave it alone.
table.insert(alsa_monitor.rules, {
  matches = { { { "device.name", "matches", "alsa_card.platform-snd_aloop*" } } },
  apply_properties = { ["device.disabled"] = true },
})
""",
    _WIREPLUMBER / "wireplumber.conf.d/51-reachy-bridge-loopback.conf": """# reachy-bridge (#219): the loopback carries the robot's speaker; leave it alone.
monitor.alsa.rules = [
  {
    matches = [ { device.name = "~alsa_card.platform-snd_aloop*" } ]
    actions = { update-props = { device.disabled = true } }
  }
]
""",
}


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
    """The tee is in ``~/.asoundrc`` and its loopback card is there."""
    try:
        return installed(ASOUNDRC.read_text(errors="ignore")) and loopback_present(CARDS.read_text())
    except OSError:
        return False


def _run(args: list[str]) -> bool:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.warning("%s failed: %s", args[0], error)
        return False
    if result.returncode != 0:
        logger.warning("%s failed: %s", args[0], (result.stderr or result.stdout).strip())
    return result.returncode == 0


def set_speaker(on: bool) -> bool:
    """Unmute (or mute) the robot's speaker for its own sounds."""
    switch = ["amixer", "-q", "-c", LOOPBACK, "cset", f"name={SPEAKER_SWITCH}", "on" if on else "off"]
    if _run(switch):
        return True
    # The switch only exists once its device has been opened: play a moment
    # of silence through it, then try again.
    silence = ["aplay", "-q", "-D", "reachy_bridge_speaker", "-t", "raw", "-f", "S16_LE", "-r", "16000", "-c", "2", "-d", "1", "/dev/zero"]
    return _run(silence) and _run(switch)


def _write(path: Path, text: str) -> None:
    """Replace ``path`` whole: ALSA never reads half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".reachy-bridge.tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


def _leave_loopback_to_us() -> None:
    written = []
    for path, rule in WIREPLUMBER_RULES.items():
        try:
            if path.read_text() == rule:
                continue
        except OSError:
            pass
        _write(path, rule)
        written.append(path)
    if not written:
        return
    # Only the user's own session manager, if it runs; otherwise the rules
    # apply whenever it next starts.
    if _run(["systemctl", "--user", "try-restart", "wireplumber.service"]):
        logger.info("Told WirePlumber to leave the ALSA loopback card alone.")
    else:
        logger.warning("Couldn't restart WirePlumber; it leaves the loopback card alone from its next start.")


def ensure() -> None:
    """Host setup: add the tee when there's a loopback card, take it out when
    there isn't, and leave the speaker unmuted. Runs after upstream's
    ``~/.asoundrc`` is written; a failure here never stops the daemon."""
    try:
        _ensure()
    except Exception as error:  # noqa: BLE001 - the robot works without it
        logger.warning("Couldn't set up hearing what Reachy says: %s", error)


def _ensure() -> None:
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
        _write(ASOUNDRC, new)
        logger.info("Added the speaker tee to ~/.asoundrc (the dashboard can hear what Reachy says).")
    elif not present and installed(text):
        _write(ASOUNDRC, untee(text))
        logger.info("No ALSA loopback card; took the speaker tee out of ~/.asoundrc.")
        # And let WirePlumber have loopback cards again.
        for path in WIREPLUMBER_RULES:
            path.unlink(missing_ok=True)
    if present and installed(ASOUNDRC.read_text(errors="ignore")):
        _leave_loopback_to_us()
        set_speaker(True)
