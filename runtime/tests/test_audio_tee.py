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

    def broken(*_args):
        raise PermissionError("read-only home")

    monkeypatch.setattr(audio_tee, "_write", broken)
    audio_tee.ensure()
    assert rc.read_text() == UPSTREAM


def test_wireplumber_rules_retry_when_the_restart_fails(tmp_path: Path, monkeypatch):
    rules = {tmp_path / "wp/rule.lua": "lua rule\n"}
    monkeypatch.setattr(audio_tee, "WIREPLUMBER_RULES", rules)
    monkeypatch.setattr(audio_tee, "_run", lambda args: False)
    audio_tee._leave_loopback_to_us()
    assert not (tmp_path / "wp/rule.lua").exists()
    monkeypatch.setattr(audio_tee, "_run", lambda args: True)
    audio_tee._leave_loopback_to_us()
    assert (tmp_path / "wp/rule.lua").read_text() == "lua rule\n"
