"""Device check (#135)."""

from __future__ import annotations

import json
from pathlib import Path

from reachy_bridge_runtime import doctor, platforms

WEBRTCSINK = "Factory Details:\n  ...\n  run-signalling-server: Whether the element should run its own signalling server\n"


def fake_root(tmp_path: Path, ids: list[str], l4t: str | None = None, rule: str | None = None) -> Path:
    for n, usb_id in enumerate(ids):
        device = tmp_path / f"sys/bus/usb/devices/1-{n}"
        device.mkdir(parents=True)
        vendor, product = usb_id.split(":")
        (device / "idVendor").write_text(vendor + "\n")
        (device / "idProduct").write_text(product + "\n")
    # A hub port without ids, like 1-0:1.0, is skipped.
    (tmp_path / "sys/bus/usb/devices/1-0:1.0").mkdir(parents=True)
    if l4t is not None:
        (tmp_path / "etc").mkdir(exist_ok=True)
        (tmp_path / "etc/nv_tegra_release").write_text(l4t)
    if rule is not None:
        (tmp_path / "etc/udev/rules.d").mkdir(parents=True)
        (tmp_path / "etc/udev/rules.d/99-reachy-mini-doa.rules").write_text(rule)
    return tmp_path


def gst(version: str | None = "gst-inspect-1.0 version 1.24.2\nGStreamer 1.24.2\n", sink: str | None = WEBRTCSINK, nice: bool = True):
    def run(args: list[str]) -> str | None:
        if args[-1] == "nicesrc":
            return "Factory Details:\n  nicesrc\n" if nice and version is not None else None
        return version if args[-1] == "--version" else sink

    return run


def by_name(results: list[doctor.Result]) -> dict[str, tuple[str, str]]:
    return {r.name: (r.status, r.detail) for r in results}


def test_os_needs_linux():
    assert doctor.os_arch("Darwin", "arm64").status == "fail"
    assert doctor.os_arch("Linux", "armv7l").status == "warn"
    assert doctor.os_arch("Linux", "aarch64").status == "pass"


def test_platform_tier():
    assert doctor.board(platforms.Platform("jetson", "tested")).status == "pass"
    assert doctor.board(platforms.Platform("generic", "experimental")).status == "warn"


def test_jetpack_reads_the_l4t_release(tmp_path: Path):
    line = "# R39 (release), REVISION: 2.1, GCID: 1, BOARD: generic, EABI: aarch64, DATE: x\n"
    assert doctor.jetpack(fake_root(tmp_path, [], l4t=line)) == doctor.Result("JetPack", "pass", "L4T R39.2.1")
    old = fake_root(tmp_path / "old", [], l4t="# R36 (release), REVISION: 4.0, GCID: 1\n")
    assert doctor.jetpack(old).status == "fail"
    assert doctor.jetpack(tmp_path / "none").status == "warn"
    assert doctor.jetpack(fake_root(tmp_path / "odd", [], l4t="something else\n")).status == "warn"


def test_gstreamer_and_webrtcsink():
    assert [r.status for r in doctor.gstreamer(gst())] == ["pass", "pass", "pass"]
    assert [r.status for r in doctor.gstreamer(gst(sink=None))] == ["pass", "fail", "pass"]
    # 0.13 and earlier have webrtcsink but no run-signalling-server.
    too_old = doctor.gstreamer(gst(sink="Factory Details:\n  webrtcsink\n"))[1]
    assert too_old.status == "fail" and "0.14" in too_old.detail
    assert doctor.gstreamer(gst(version="GStreamer 1.22.0\n"))[0].status == "fail"
    assert [r.status for r in doctor.gstreamer(gst(version=None))] == ["fail", "skip", "skip"]


def test_libnice_is_required_for_webrtc():
    nice = doctor.gstreamer(gst(nice=False))[2]
    assert nice.status == "fail" and "gstreamer1.0-nice" in nice.detail


def test_robot_usb_devices(tmp_path: Path):
    root = fake_root(tmp_path, [doctor.SERIAL, doctor.AUDIO, "0bda:5489"])
    results = by_name(doctor.robot_usb(doctor.usb_ids(root)))
    assert results["Motor bus"][0] == "pass"
    assert results["Audio"][0] == "pass"
    assert results["Camera"][0] == "fail"
    # Older parts the daemon still runs with are a warning, not a cable problem.
    old = by_name(doctor.robot_usb({doctor.SERIAL, doctor.OLD_AUDIO, doctor.OLD_CAMERA}))
    assert old["Audio"][0] == "warn" and "firmware" in old["Audio"][1]
    assert old["Camera"][0] == "warn"


def test_group_membership():
    members = {"dialout": ["andry"], "plugdev": []}.get
    assert doctor.group("dialout", "andry", {"dialout"}, members, "why").status == "pass"
    # Added, but this login doesn't have it yet.
    assert doctor.group("dialout", "andry", set(), members, "why").status == "warn"
    missing = doctor.group("plugdev", "andry", set(), members, "why")
    assert missing.status == "fail" and "usermod -aG plugdev andry" in missing.detail
    assert doctor.group("nosuch", "andry", set(), members, "why").status == "fail"


def test_udev_rule(tmp_path: Path):
    rule = 'SUBSYSTEM=="usb", ATTR{idVendor}=="38fb", ATTR{idProduct}=="1001", MODE="0660", GROUP="plugdev"\n'
    assert doctor.udev_rule(fake_root(tmp_path, [], rule=rule)).status == "pass"
    assert doctor.udev_rule(tmp_path / "empty").status == "fail"
    parent = 'SUBSYSTEM=="usb", ATTRS{idVendor} == "38fb", ATTRS{idProduct}=="1001", MODE="0660"\n'
    assert doctor.udev_rule(fake_root(tmp_path / "attrs", [], rule=parent)).status == "pass"
    assert doctor.udev_rule(fake_root(tmp_path / "commented", [], rule="# " + rule)).status == "fail"
    split = 'ATTR{idVendor}=="38fb", MODE="0660"\nATTR{idProduct}=="1001", MODE="0660"\n'
    assert doctor.udev_rule(fake_root(tmp_path / "split", [], rule=split)).status == "fail"
    # A same-named file in /etc masks the one under /lib.
    masked = fake_root(tmp_path / "masked", [], rule="")
    (masked / "lib/udev/rules.d").mkdir(parents=True)
    (masked / "lib/udev/rules.d/99-reachy-mini-doa.rules").write_text(rule)
    assert doctor.udev_rule(masked).status == "fail"
    latin1 = fake_root(tmp_path / "latin1", [], rule="")
    (latin1 / "etc/udev/rules.d/10-vendor.rules").write_bytes(b"# caf\xe9\n")
    (latin1 / "etc/udev/rules.d/99-reachy-mini-doa.rules").write_text(rule)
    assert doctor.udev_rule(latin1).status == "pass"


def test_memory(tmp_path: Path):
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc/meminfo").write_text("MemTotal:        7814036 kB\nMemFree: 1 kB\n")
    assert doctor.memory(tmp_path) == doctor.Result("Memory", "pass", "7.8 GB")
    (tmp_path / "proc/meminfo").write_text("MemTotal:        2000000 kB\n")
    assert doctor.memory(tmp_path).status == "warn"


def test_main_exits_1_on_a_failure(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "run_checks", lambda: [doctor.Result("OS", "pass", "Linux"), doctor.Result("Camera", "fail", "x")])
    assert doctor.main([]) == 1
    assert "fail  Camera" in capsys.readouterr().out
    monkeypatch.setattr(doctor, "run_checks", lambda: [doctor.Result("Disk", "warn", "low")])
    assert doctor.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"name": "Disk", "status": "warn", "detail": "low"}]


def test_run_checks_never_powers_the_robot(tmp_path: Path, monkeypatch):
    calls: list[list[str]] = []

    def run(args: list[str]) -> str | None:
        calls.append(args)
        return gst()(args)

    monkeypatch.setattr(doctor._platform, "system", lambda: "Linux")
    monkeypatch.setattr(doctor._platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(doctor.platforms, "detect", lambda: platforms.Platform("jetson", "tested"))
    monkeypatch.setenv("USER", "someone-else")
    line = "# R39 (release), REVISION: 2.1, GCID: 1\n"
    results = by_name(doctor.run_checks(fake_root(tmp_path, [doctor.SERIAL], l4t=line), run))
    assert {args[0] for args in calls} == {"gst-inspect-1.0"}
    assert results["Power supply"][0] == "skip"
    assert results["Motor bus"][0] == "pass"
    assert results["JetPack"][0] == "pass"
    # Groups are checked for the uid's account, not $USER.
    assert "someone-else" not in results["dialout group"][1]


def test_loopback_card(tmp_path: Path):
    (tmp_path / "proc/asound").mkdir(parents=True)
    cards = tmp_path / "proc/asound/cards"
    cards.write_text(" 0 [Audio          ]: USB-Audio - Reachy Mini Audio\n")
    missing = doctor.loopback(tmp_path)
    assert missing.status == "warn" and "snd-aloop" in missing.detail
    cards.write_text(cards.read_text() + "10 [Loopback       ]: Loopback - Loopback\n                      Loopback 1\n")
    assert doctor.loopback(tmp_path).status == "pass"
    assert doctor.loopback(tmp_path / "nowhere").status == "warn"
