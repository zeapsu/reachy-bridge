"""Device check (#135): can this board run the bridge and the robot?

    reachy-bridge-runtime --doctor [--json]

Prints pass / warn / fail / skip per item and exits 1 if anything failed.
Read-only: it never powers the robot on or changes the system. Whether the
robot's power supply is connected needs the motors on, so the setup's on/off
test checks that instead.
"""

from __future__ import annotations

import grp
import json
import os
import platform as _platform
import pwd
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from reachy_bridge_runtime import audio_tee, platforms

# The Lite's USB devices: the motor bus's serial adapter, then Pollen's audio
# and camera (reachy_mini's daemon/utils.py and media/camera_constants.py).
SERIAL = "1a86:55d3"
AUDIO = "38fb:1001"
CAMERA = "38fb:1002"
# Older parts the daemon still accepts: the ReSpeaker audio firmware
# (media/audio_control_utils.py) and the earlier camera (OlderRPiCamSpecs).
OLD_AUDIO = "2886:001a"
OLD_CAMERA = "1bcf:28c4"
# JetPack 7 is L4T R38 and later.
MIN_L4T = 38
MIN_GSTREAMER = (1, 24)
# The bridge's install with its venvs, and room for a few community apps.
MIN_DISK_GB, LOW_DISK_GB = 3, 8
LOW_RAM_GB = 4


@dataclass(frozen=True)
class Result:
    name: str
    status: str  # pass, warn, fail or skip
    detail: str


def _run(args: list[str]) -> str | None:
    """stdout of a command, or None if it is missing or fails."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def os_arch(system: str, machine: str) -> Result:
    if system != "Linux":
        return Result("OS", "fail", f"{system} {machine}; the bridge runs on Linux")
    if machine not in ("aarch64", "x86_64"):
        return Result("OS", "warn", f"Linux {machine}; only aarch64 and x86_64 are built")
    return Result("OS", "pass", f"Linux {machine}")


def board(platform: platforms.Platform) -> Result:
    if platform.tier == "tested":
        return Result("Platform", "pass", f"{platform.name} (tested)")
    return Result("Platform", "warn", f"{platform.name} (experimental: other boards may need fixes)")


def jetpack(root: Path) -> Result:
    try:
        first = (root / "etc/nv_tegra_release").read_text(errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return Result("JetPack", "warn", "no /etc/nv_tegra_release; can't tell the JetPack version")
    match = re.search(r"R(\d+) \(release\), REVISION: ([\d.]+)", first)
    if not match:
        return Result("JetPack", "warn", f"unrecognised release line: {first.strip()}")
    release = f"L4T R{match[1]}.{match[2]}"
    if int(match[1]) < MIN_L4T:
        return Result("JetPack", "fail", f"{release}; JetPack 7 (L4T R{MIN_L4T}+) is needed")
    return Result("JetPack", "pass", release)


def gstreamer(run: Callable[[list[str]], str | None]) -> list[Result]:
    version = run(["gst-inspect-1.0", "--version"])
    if version is None:
        hint = "install gstreamer1.0-tools and the GStreamer plugins"
        return [
            Result("GStreamer", "fail", f"gst-inspect-1.0 not found; {hint}"),
            Result("webrtcsink", "skip", "needs GStreamer"),
            Result("libnice", "skip", "needs GStreamer"),
        ]
    match = re.search(r"GStreamer (\d+)\.(\d+)\.(\d+)", version)
    if not match:
        found = Result("GStreamer", "warn", "couldn't read the version")
    elif (int(match[1]), int(match[2])) < MIN_GSTREAMER:
        found = Result("GStreamer", "fail", f"{match[0]}; 1.24 or later is needed")
    else:
        found = Result("GStreamer", "pass", match[0])
    sink = run(["gst-inspect-1.0", "webrtcsink"])
    build = "bash tools/build_webrtcsink_jetson.sh"
    if sink is None:
        webrtc = Result("webrtcsink", "fail", f"not installed; camera and audio streaming need it ({build})")
    elif "run-signalling-server" not in sink:
        webrtc = Result("webrtcsink", "fail", f"too old (no run-signalling-server); gst-plugins-rs 0.14 is needed ({build})")
    else:
        webrtc = Result("webrtcsink", "pass", "gst-plugins-rs with run-signalling-server")
    # WebRTC (webrtcbin, inside webrtcsink and the audio gateway) refuses every
    # stream without libnice's GStreamer elements.
    if run(["gst-inspect-1.0", "nicesrc"]) is None:
        nice = Result("libnice", "fail", "GStreamer's libnice plugin is missing; WebRTC needs it (sudo apt install gstreamer1.0-nice)")
    else:
        nice = Result("libnice", "pass", "nicesrc")
    return [found, webrtc, nice]


def usb_ids(root: Path) -> set[str]:
    ids = set()
    for device in (root / "sys/bus/usb/devices").glob("*"):
        try:
            vendor = (device / "idVendor").read_text(errors="replace").strip()
            ids.add(f"{vendor}:{(device / 'idProduct').read_text(errors='replace').strip()}")
        except OSError:
            continue
    return ids


def robot_usb(ids: set[str]) -> list[Result]:
    results = []
    for name, usb_id, old, old_note in (
        ("Motor bus", SERIAL, None, ""),
        ("Audio", AUDIO, OLD_AUDIO, "old ReSpeaker firmware; Pollen recommends updating it"),
        ("Camera", CAMERA, OLD_CAMERA, "the older camera; it works"),
    ):
        if usb_id in ids:
            results.append(Result(name, "pass", f"USB {usb_id}"))
        elif old in ids:
            results.append(Result(name, "warn", f"USB {old}: {old_note}"))
        else:
            results.append(Result(name, "fail", f"USB {usb_id} not found; check the robot's USB cable"))
    return results


def group(name: str, user: str, active: set[str], members: Callable[[str], list[str] | None], why: str) -> Result:
    listed = members(name)
    if listed is None:
        return Result(f"{name} group", "fail", f"no {name} group on this system; {why}")
    if name in active:
        return Result(f"{name} group", "pass", f"{user} is a member")
    if user in listed:
        return Result(f"{name} group", "warn", f"{user} was added; log out and back in for it to apply")
    return Result(f"{name} group", "fail", f"{user} isn't a member ({why}): sudo usermod -aG {name} {user}")


_VENDOR = re.compile(r'ATTRS?\{idVendor\}\s*==\s*"38fb"')
_PRODUCT = re.compile(r'ATTRS?\{idProduct\}\s*==\s*"1001"')


def udev_rule(root: Path) -> Result:
    # udev reads one file per name, /etc first: a file there (even a
    # /dev/null mask) hides the same name under /lib.
    seen = set()
    for rules in ("etc/udev/rules.d", "run/udev/rules.d", "lib/udev/rules.d", "usr/lib/udev/rules.d"):
        for rule in sorted((root / rules).glob("*.rules")):
            if rule.name in seen:
                continue
            seen.add(rule.name)
            try:
                lines = rule.read_text(errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                if not line.lstrip().startswith("#") and _VENDOR.search(line) and _PRODUCT.search(line):
                    return Result("udev rule", "pass", f"/{rules}/{rule.name}")
    return Result(
        "udev rule",
        "fail",
        "no rule for the robot's audio device; direction of arrival fails "
        "(sudo cp tools/udev/99-reachy-mini-doa.rules /etc/udev/rules.d/)",
    )


def loopback(root: Path) -> Result:
    """The ALSA loopback card that lets the dashboard hear what the robot says."""
    try:
        cards = (root / "proc/asound/cards").read_text(errors="replace")
    except OSError:
        return Result("Audio loopback", "warn", "couldn't read /proc/asound/cards")
    if audio_tee.loopback_present(cards):
        if not (root / f"proc/asound/{audio_tee.LOOPBACK}/pcm0p/sub1").exists():
            return Result("Audio loopback", "warn", "the loopback card has one substream; hearing Reachy and talking to its apps need two (pcm_substreams=2)")
        return Result("Audio loopback", "pass", f"ALSA card {audio_tee.LOOPBACK}")
    return Result(
        "Audio loopback",
        "warn",
        "no ALSA loopback card; hearing what Reachy says and talking to its apps need it "
        f"(sudo modprobe snd-aloop id={audio_tee.LOOPBACK}, and load it at boot)",
    )


def disk(home: Path) -> Result:
    try:
        free = shutil.disk_usage(home).free / 1e9
    except OSError:
        return Result("Disk", "warn", f"couldn't read free space on {home}")
    if free < MIN_DISK_GB:
        return Result("Disk", "fail", f"{free:.1f} GB free; {MIN_DISK_GB} GB needed")
    if free < LOW_DISK_GB:
        return Result("Disk", "warn", f"{free:.1f} GB free; apps may run out of room")
    return Result("Disk", "pass", f"{free:.1f} GB free")


def memory(root: Path) -> Result:
    try:
        meminfo = (root / "proc/meminfo").read_text(errors="replace")
        total = int(re.search(r"MemTotal:\s+(\d+) kB", meminfo)[1]) / 1e6  # type: ignore[index]
    except (OSError, TypeError):
        return Result("Memory", "warn", "couldn't read /proc/meminfo")
    if total < LOW_RAM_GB:
        return Result("Memory", "warn", f"{total:.1f} GB; apps with local models may not fit")
    return Result("Memory", "pass", f"{total:.1f} GB")


def _members(name: str) -> list[str] | None:
    try:
        entry = grp.getgrnam(name)
    except KeyError:
        return None
    members = list(entry.gr_mem)
    try:
        user = pwd.getpwuid(os.getuid())
    except KeyError:
        return members
    if user.pw_gid == entry.gr_gid:
        members.append(user.pw_name)
    return members


def _user() -> str:
    # The uid, not $USER, which sudo -E or su -m leave pointing at someone else.
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())


def _active_groups() -> set[str]:
    names = set()
    for gid in os.getgroups():
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    return names


def run_checks(root: Path = Path("/"), run: Callable[[list[str]], str | None] = _run) -> list[Result]:
    results = [os_arch(_platform.system(), _platform.machine())]
    if results[0].status == "fail":
        return results
    platform = platforms.detect()
    results.append(board(platform))
    if platform.name == "jetson":
        results.append(jetpack(root))
    results += gstreamer(run)
    results += robot_usb(usb_ids(root))
    user = _user()
    active = _active_groups()
    results.append(group("dialout", user, active, _members, "the motor bus's serial port needs it"))
    results.append(group("plugdev", user, active, _members, "the udev rule gives it the audio device"))
    results.append(udev_rule(root))
    results.append(loopback(root))
    results.append(disk(Path.home()))
    results.append(memory(root))
    results.append(Result("Power supply", "skip", "needs the motors on; the setup's on/off test checks it"))
    return results


def main(argv: list[str]) -> int:
    results = run_checks()
    if "--json" in argv:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        print("Device check")
        width = max(len(r.name) for r in results)
        for r in results:
            print(f"  {r.status:<4}  {r.name:<{width}}  {r.detail}")
    return 1 if any(r.status == "fail" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
