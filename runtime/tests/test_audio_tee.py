from pathlib import Path

from reachy_bridge_runtime import audio_tee

# What upstream's write_asoundrc_to_home() writes (reachy_mini 1.12.0).
UPSTREAM = """
pcm.!default {
    type hw
    card 0
}

ctl.!default {
    type hw
    card 0
}

pcm.reachymini_audio_sink {
    type dmix
    ipc_key 4241
    slave {
        pcm "hw:0,0"
        channels 2
        period_size 256
        buffer_size 1024
        rate 16000
    }
    bindings {
        0 0
        1 1
    }
}

pcm.reachymini_audio_src {
    type dsnoop
    ipc_key 4242
    slave {
        pcm "hw:0,0"
        channels 2
        rate 16000
        period_size 256
        buffer_size 1024
    }
}
"""

CARDS = """ 0 [Audio          ]: USB-Audio - Reachy Mini Audio
                      Pollen Robotics Reachy Mini Audio at usb-3610000.usb-2.1.1, high speed
10 [Loopback       ]: Loopback - Loopback
                      Loopback 1
"""


def test_tee_keeps_upstreams_names_and_round_trips():
    teed = audio_tee.tee(UPSTREAM)
    assert teed is not None and audio_tee.installed(teed)
    # Upstream's check looks for both names and the card number.
    assert "pcm.reachymini_audio_sink {" in teed and "pcm.reachymini_audio_src {" in teed and "card 0" in teed
    # The speaker's dmix keeps its ipc_key and hardware under its new name.
    speaker = teed[teed.index(f"pcm.{audio_tee.SPEAKER}") :]
    assert speaker.index('ipc_key 4241') < speaker.index('pcm "hw:0,0"') < speaker.index("pcm.reachymini_audio_src")
    assert teed.count("pcm.reachymini_audio_sink {") == 1
    assert audio_tee.tee(teed) == teed
    assert audio_tee.untee(teed).strip() == UPSTREAM.strip()
    assert audio_tee.untee(UPSTREAM) == UPSTREAM


def test_tee_leaves_the_mic_device_as_upstream_wrote_it():
    teed = audio_tee.tee(UPSTREAM)
    assert teed is not None
    mic = audio_tee._block(teed, audio_tee.SOURCE)
    plain = audio_tee._block(UPSTREAM, audio_tee.SOURCE)
    assert mic is not None and plain is not None
    assert teed[mic[0] : mic[1]] == UPSTREAM[plain[0] : plain[1]]
    assert "reachy_bridge_mics" not in teed


def test_an_older_tee_with_the_mic_mix_goes_back_to_upstreams_mic():
    old = audio_tee._rename(UPSTREAM.replace("pcm.reachymini_audio_sink", f"pcm.{audio_tee.SPEAKER}"), audio_tee.SOURCE, audio_tee.OLD_MIC)
    old += "\n# reachy-bridge: speaker tee (#219), and mic tee (v2)\npcm.reachymini_audio_src { type plug }\n"
    assert audio_tee.untee(old).strip() == UPSTREAM.strip()
    new = audio_tee.tee(old)
    assert new is not None and audio_tee.installed(new) and audio_tee.OLD_MIC not in new


def test_an_older_tee_is_replaced():
    v1 = audio_tee.untee(audio_tee.tee(UPSTREAM)).rstrip("\n").replace(
        "pcm.reachymini_audio_sink {", "pcm.reachy_bridge_speaker_hw {", 1
    ) + "\n\n# reachy-bridge: speaker tee (#219)\npcm.reachymini_audio_sink { type plug slave.pcm \"x\" }\n"
    assert not audio_tee.installed(v1)
    assert audio_tee.untee(v1).strip() == UPSTREAM.strip()
    assert audio_tee.tee(v1) == audio_tee.tee(UPSTREAM)


def test_the_first_version_leaves_this_tee_alone():
    # A failed deploy rolls back to it; it looks for its own marker.
    assert "# reachy-bridge: speaker tee (#219)" in audio_tee.MARKER


def test_a_card_without_a_second_subdevice_gets_no_tee(tmp_path: Path, monkeypatch):
    rc, cards = tmp_path / ".asoundrc", tmp_path / "cards"
    rc.write_text(UPSTREAM)
    cards.write_text(CARDS)
    monkeypatch.setattr(audio_tee, "ASOUNDRC", rc)
    monkeypatch.setattr(audio_tee, "CARDS", cards)
    monkeypatch.setattr(audio_tee, "SECOND_SUBDEVICE", tmp_path / "missing")
    audio_tee.ensure()
    assert rc.read_text() == UPSTREAM


def test_no_upstream_sink_no_tee():
    assert audio_tee.tee("pcm.!default { type hw card 1 }\n") is None


def test_loopback_present():
    assert audio_tee.loopback_present(CARDS)
    assert not audio_tee.loopback_present(CARDS.split("10 [")[0])
    # Another card that only mentions the word isn't it.
    assert not audio_tee.loopback_present(" 1 [HDA            ]: tegra-hda - Loopback\n")


def test_ensure_adds_and_removes_the_tee(tmp_path: Path, monkeypatch):
    rc, cards = tmp_path / ".asoundrc", tmp_path / "cards"
    rules = {tmp_path / "wp/main.lua.d/rule.lua": "lua rule\n", tmp_path / "wp/wireplumber.conf.d/rule.conf": "conf rule\n"}
    monkeypatch.setattr(audio_tee, "ASOUNDRC", rc)
    monkeypatch.setattr(audio_tee, "CARDS", cards)
    monkeypatch.setattr(audio_tee, "SECOND_SUBDEVICE", tmp_path)
    monkeypatch.setattr(audio_tee, "WIREPLUMBER_RULES", rules)
    switched: list[bool] = []
    ran: list[list[str]] = []
    monkeypatch.setattr(audio_tee, "set_speaker", lambda on: switched.append(on) or True)
    monkeypatch.setattr(audio_tee, "_run", lambda args: ran.append(args) or True)
    # No ~/.asoundrc (a board without upstream's): nothing to do.
    cards.write_text(CARDS)
    audio_tee.ensure()
    assert not rc.exists() and switched == [] and ran == []
    rc.write_text(UPSTREAM)
    audio_tee.ensure()
    assert audio_tee.is_installed() and switched == [True]
    assert all(path.read_text() == rule for path, rule in rules.items())
    assert ran == [["systemctl", "--user", "try-restart", "wireplumber.service"]]
    assert not list(tmp_path.glob("**/*.tmp"))
    # Already in place: WirePlumber isn't restarted again.
    audio_tee.ensure()
    assert len(ran) == 1 and switched == [True, True]
    cards.write_text(CARDS.split("10 [")[0])
    assert not audio_tee.is_installed()
    audio_tee.ensure()
    assert rc.read_text().strip() == UPSTREAM.strip() and switched == [True, True]
    assert not any(path.exists() for path in rules)


def test_set_speaker_creates_the_switch_once(monkeypatch):
    calls: list[str] = []
    answers = iter([False, True, True])
    monkeypatch.setattr(audio_tee, "_run", lambda args: calls.append(args[0]) or next(answers))
    # The switch isn't there yet: a moment of silence makes it, then it's set.
    assert audio_tee.set_speaker(False)
    assert calls == ["amixer", "aplay", "amixer"]
    calls.clear()
    monkeypatch.setattr(audio_tee, "_run", lambda args: calls.append(args) or True)
    assert audio_tee.set_speaker(True)
    assert calls == [["amixer", "-q", "-c", "Loopback", "cset", "name=Reachy Bridge Speaker", "on"]]


def test_run_reports_failures(monkeypatch):
    import subprocess

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("amixer")

    monkeypatch.setattr(subprocess, "run", missing)
    assert not audio_tee._run(["amixer"])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "no such control"))
    assert not audio_tee._run(["amixer"])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    assert audio_tee._run(["amixer"])


def test_ensure_never_stops_the_daemon(tmp_path: Path, monkeypatch):
    rc = tmp_path / ".asoundrc"
    rc.write_text(UPSTREAM)
    (tmp_path / "cards").write_text(CARDS)
    monkeypatch.setattr(audio_tee, "ASOUNDRC", rc)
    monkeypatch.setattr(audio_tee, "CARDS", tmp_path / "cards")
    monkeypatch.setattr(audio_tee, "SECOND_SUBDEVICE", tmp_path)

    def broken(*_args):
        raise PermissionError("read-only home")

    monkeypatch.setattr(audio_tee, "_write", broken)
    audio_tee.ensure()
    assert rc.read_text() == UPSTREAM


def test_wireplumber_rules_stay_when_the_restart_fails(tmp_path: Path, monkeypatch):
    rules = {tmp_path / "wp/rule.lua": "lua rule\n"}
    monkeypatch.setattr(audio_tee, "WIREPLUMBER_RULES", rules)
    ran: list[list[str]] = []
    monkeypatch.setattr(audio_tee, "_run", lambda args: ran.append(args) or False)
    audio_tee._leave_loopback_to_us()
    # They apply when WirePlumber next starts; not rewritten every time.
    audio_tee._leave_loopback_to_us()
    assert (tmp_path / "wp/rule.lua").read_text() == "lua rule\n" and len(ran) == 1


def _ttable(teed: str, name: str) -> str:
    block = audio_tee._block(teed, name)
    assert block is not None
    return teed[block[0] : block[1]]


def test_talk_reaches_the_wobble_but_not_hearing():
    teed = audio_tee.tee(UPSTREAM)
    assert teed is not None
    # Talk writes the loopback's last two channels, the robot's audio the first two.
    assert "bindings { 0 2 1 3 }" in _ttable(teed, "reachy_bridge_loop_talk")
    assert "bindings { 0 0 1 1 }" in _ttable(teed, "reachy_bridge_loop_out")
    assert "0.0 1 1.1 1 }" in _ttable(teed, audio_tee.HEAR_SOURCE)
    # Robot audio on the wobble's left channel, Talk on its right.
    assert "0.0 0.5 0.1 0.5 1.2 0.5 1.3 0.5" in _ttable(teed, audio_tee.WOBBLE_SOURCE)
    assert "reachy_bridge_talk_both" in _ttable(teed, audio_tee.TALK_SINK)


def test_tee_marker_keeps_earlier_versions_markers():
    assert "speaker tee (#219)" in audio_tee.MARKER and "and mic tee (v2)" in audio_tee.MARKER and "(v3)" in audio_tee.MARKER and "(v4)" in audio_tee.MARKER
