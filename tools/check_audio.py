#!/usr/bin/env python3
"""Check the robot's audio routing on the device itself (#219): the ALSA side
that CI can't run, since its runners have no sound card.

    python3 tools/check_audio.py

Run it on the robot's computer, from the checkout, with the bridge installed,
the robot off and nobody listening or talking. It plays a short test tone into
each of the tee's inputs and records its outputs, and checks each tone turns up
where ``audio_tee`` routes it and nowhere else:

- what the robot plays reaches the gateway's "hear" capture, and the head
  wobble's left channel;
- Talk reaches the wobble's right channel, but not "hear" (a browser
  mustn't hear itself);
- a browser's voice for apps reaches what apps hear as the mic (through the mic
  mixer), but not the real mic the gateway listens to;
- the speaker mute switch turns off and back on.

What the robot plays is checked with the speaker muted, so it stays quiet.
Talk can't be muted: it plays two quiet one-second beeps on the speaker. Uses
only aplay, arecord and amixer, no robot motion. Exit code 0 means every
check passed.
"""

from __future__ import annotations

import argparse
import math
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "src"))
from reachy_bridge_runtime import audio_tee  # noqa: E402

RATE = 16000
SECONDS = 1.0
# The tones: what the robot plays, Talk, and a browser's voice for apps.
TONES = {"robot": 700, "talk": 1100, "voice": 1300}
HEARD = 0.01
SILENT = 0.003
# Talk plays on the speaker for real: keep it quiet.
VOLUME = {"robot": 0.25, "talk": 0.05, "voice": 0.25}
GATEWAY_UNIT = "reachy-bridge-audio.service"


def tone(freq: int, volume: float, seconds: float, channels: int = 2) -> bytes:
    frames = int(RATE * seconds)
    out = bytearray()
    for i in range(frames):
        sample = int(volume * 32767 * math.sin(2 * math.pi * freq * i / RATE))
        out += struct.pack("<h", sample) * channels
    return bytes(out)


def amplitude(samples: list[float], freq: int) -> float:
    """Hann-windowed Goertzel: the tone's amplitude, 0 to 1."""
    n = len(samples)
    if n < 2:
        return 0.0
    c = 2 * math.cos(2 * math.pi * freq / RATE)
    s1 = s2 = gain = 0.0
    for i, x in enumerate(samples):
        w = 0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1))
        gain += w
        s1, s2 = x * w + c * s1 - s2, s1
    return 2 * math.sqrt(max(s1 * s1 + s2 * s2 - c * s1 * s2, 0.0)) / gain


def channels_of(raw: bytes, channels: int) -> list[list[float]]:
    count = len(raw) // (2 * channels)
    values = struct.unpack(f"<{count * channels}h", raw[: count * 2 * channels])
    return [[v / 32768 for v in values[c::channels]] for c in range(channels)]


def play_and_record(
    play_device: str, sound: bytes, record_device: str, channels: int
) -> list[list[float]]:
    """Record ``record_device`` while ``sound`` plays on ``play_device``; the
    middle of the recording, per channel."""
    # The robot or a browser may have come on since: either could turn the
    # tones into head motion, or have its speaker mute undone.
    why = preflight()
    if why:
        raise Refused(why)
    record = subprocess.Popen(
        [
            "arecord",
            "-q",
            "-D",
            record_device,
            "-t",
            "raw",
            "-f",
            "S16_LE",
            "-r",
            str(RATE),
            "-c",
            str(channels),
            "-d",
            str(int(SECONDS + 2)),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.5)
    player = subprocess.run(
        [
            "aplay",
            "-q",
            "-D",
            play_device,
            "-t",
            "raw",
            "-f",
            "S16_LE",
            "-r",
            str(RATE),
            "-c",
            "2",
        ],
        input=sound,
        capture_output=True,
        timeout=10,
    )
    raw, err = record.communicate(timeout=15)
    if player.returncode != 0:
        raise RuntimeError(f"aplay -D {play_device}: {player.stderr.decode().strip()}")
    if record.returncode != 0:
        raise RuntimeError(f"arecord -D {record_device}: {err.decode().strip()}")
    # The tone starts 0.5 s in: measure its middle.
    start, length = int(0.7 * RATE), int((SECONDS - 0.4) * RATE)
    measured = [ch[start : start + length] for ch in channels_of(raw, channels)]
    # A short recording would pass every "doesn't reach" check.
    if len(measured) != channels or any(len(ch) < length for ch in measured):
        raise RuntimeError(f"arecord -D {record_device}: recording too short")
    return measured


class Refused(Exception):
    """It isn't safe to go on."""


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def expect(self, what: str, level: float, present: bool) -> None:
        ok = level > HEARD if present else level < SILENT
        self.failed += not ok
        print(f"{'ok' if ok else 'FAIL'}: {what} ({level:.4f})")

    def check(self, what: str, ok: bool) -> None:
        self.failed += not ok
        print(f"{'ok' if ok else 'FAIL'}: {what}")


def gateway_active() -> bool:
    return (
        subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", GATEWAY_UNIT]
        ).returncode
        == 0
    )


def preflight() -> str | None:
    """Why this can't run now, or None."""
    if not audio_tee.is_installed():
        return "the speaker and mic tee isn't installed (no ALSA loopback card?); see reachy-bridge check"
    cli = Path.home() / ".local/bin/reachy-bridge"
    if not cli.exists():
        return f"{cli} isn't installed"
    power = subprocess.run([str(cli), "robot-power"], capture_output=True, text=True)
    if power.stdout.strip() != "off":
        return f"the robot isn't confirmed off ({power.stdout.strip() or power.stderr.strip()}); turn it off first"
    if gateway_active():
        return f"{GATEWAY_UNIT} is running: someone may be listening or talking; stop it from the dashboard"
    if (
        subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", "reachy-bridge-mic.service"]
        ).returncode
        != 0
    ):
        return "the mic mixer (reachy-bridge-mic.service) isn't running"
    return None


def main() -> int:
    argparse.ArgumentParser(description=__doc__.split("\n\n")[0]).parse_args()
    why = preflight()
    if why:
        print(f"check_audio: {why}", file=sys.stderr)
        return 2
    checks = Checks()

    # A dropped ssh session or a stop must still put the speaker mute back.
    def stop(signum: int, _frame: object) -> None:
        raise Refused(f"stopped by signal {signum}")

    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, stop)
    try:
        # What the robot plays, with the speaker muted as while a browser hears it.
        muted = audio_tee.set_speaker(False) and not audio_tee.speaker_on()
        checks.check("the speaker mute switch turns off", muted)
        if not muted:
            # The robot's tones would play out loud.
            raise Refused("the speaker didn't mute")
        robot = tone(TONES["robot"], VOLUME["robot"], SECONDS)
        hear = play_and_record(audio_tee.SINK, robot, audio_tee.HEAR_SOURCE, 2)
        checks.expect(
            "what the robot plays reaches 'hear'",
            amplitude(hear[0], TONES["robot"]),
            True,
        )
        wobble = play_and_record(audio_tee.SINK, robot, audio_tee.WOBBLE_SOURCE, 2)
        checks.expect(
            "what the robot plays reaches the wobble's left channel",
            amplitude(wobble[0], TONES["robot"]),
            True,
        )
        checks.expect(
            "...and not its right (Talk) channel",
            amplitude(wobble[1], TONES["robot"]),
            False,
        )
        apps = play_and_record(audio_tee.SINK, robot, audio_tee.SOURCE, 2)
        checks.expect(
            "what the robot plays doesn't reach apps' mic",
            amplitude(apps[0], TONES["robot"]),
            False,
        )

        # Talk: on the speaker (quietly), never back to 'hear'.
        talk = tone(TONES["talk"], VOLUME["talk"], SECONDS)
        hear = play_and_record(audio_tee.TALK_SINK, talk, audio_tee.HEAR_SOURCE, 2)
        checks.expect(
            "Talk doesn't reach 'hear'", amplitude(hear[0], TONES["talk"]), False
        )
        wobble = play_and_record(audio_tee.TALK_SINK, talk, audio_tee.WOBBLE_SOURCE, 2)
        checks.expect(
            "Talk reaches the wobble's right channel",
            amplitude(wobble[1], TONES["talk"]),
            True,
        )
        checks.expect(
            "...and not its left channel", amplitude(wobble[0], TONES["talk"]), False
        )

        # A browser's voice for apps: into what apps hear, not the real mic.
        voice = tone(TONES["voice"], VOLUME["voice"], SECONDS)
        apps = play_and_record(audio_tee.APP_VOICE_SINK, voice, audio_tee.SOURCE, 2)
        checks.expect(
            "a browser's voice reaches apps' mic (mic mixer)",
            amplitude(apps[0], TONES["voice"]),
            True,
        )
        mic = play_and_record(audio_tee.APP_VOICE_SINK, voice, audio_tee.MIC, 2)
        checks.expect(
            "...and not the real mic the gateway listens to",
            amplitude(mic[0], TONES["voice"]),
            False,
        )
        hear = play_and_record(
            audio_tee.APP_VOICE_SINK, voice, audio_tee.HEAR_SOURCE, 2
        )
        checks.expect("...nor 'hear'", amplitude(hear[0], TONES["voice"]), False)
    except Refused as why:
        checks.check(f"stopped early: {why}", False)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        checks.check(f"audio devices work ({error})", False)
    finally:
        # A second signal (or Ctrl-C) mustn't skip putting the switch back.
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)
        # With no browser session the speaker is always on, so put it on: that
        # also mends a check that was killed. A session that started since
        # owns the switch.
        if gateway_active():
            print("the audio gateway started: it keeps the speaker mute switch")
        else:
            checks.check(
                "the speaker mute switch is back on",
                audio_tee.set_speaker(True) and audio_tee.speaker_on(),
            )
    version_file = Path.home() / ".local/share/reachy-bridge/VERSION"
    version = version_file.read_text().strip() if version_file.exists() else "unknown"
    print(
        f"\naudio device check {'passed' if not checks.failed else f'FAILED ({checks.failed})'} on the installed {version}"
    )
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
