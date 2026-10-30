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
    monkeypatch.setattr(audio_tee, "ASOUNDRC", rc)
    monkeypatch.setattr(audio_tee, "CARDS", cards)
    switched: list[bool] = []
    monkeypatch.setattr(audio_tee, "set_speaker", lambda on: switched.append(on) or True)
    # No ~/.asoundrc (a board without upstream's): nothing to do.
    audio_tee.ensure()
    assert not rc.exists() and switched == []
    rc.write_text(UPSTREAM)
    cards.write_text(CARDS)
    audio_tee.ensure()
    assert audio_tee.is_installed() and switched == [True]
    cards.write_text(CARDS.split("10 [")[0])
    audio_tee.ensure()
    assert rc.read_text().strip() == UPSTREAM.strip() and switched == [True]
