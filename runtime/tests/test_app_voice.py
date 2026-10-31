"""Browser voice added to an app's mic read (#219)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

from reachy_bridge_runtime.features import app_voice, app_voice_shim as shim


def frames(*values: float) -> bytes:
    return np.array([[v, v] for v in values], dtype=np.float32).tobytes()


def test_buffer_returns_what_was_fed_in_order_across_chunks():
    buffer = shim.VoiceBuffer()
    buffer.feed(frames(1, 2))
    buffer.feed(frames(3))
    assert buffer.take(shim.BYTES_PER_FRAME) == frames(1)
    assert buffer.take(10 * shim.BYTES_PER_FRAME) == frames(2, 3)
    assert buffer.take(shim.BYTES_PER_FRAME) == b""


def test_buffer_drops_the_oldest_past_its_limit():
    buffer = shim.VoiceBuffer(limit=2 * shim.BYTES_PER_FRAME)
    for value in (1, 2, 3):
        buffer.feed(frames(value))
    assert buffer.take(10 * shim.BYTES_PER_FRAME) == frames(2, 3)


def test_mix_adds_voice_clips_and_keeps_a_short_read_aligned():
    samples = np.array([[0.5, 0.5], [0.25, 0.25]], dtype=np.float32)
    mixed = shim.mix(samples, frames(0.75))
    assert mixed.tolist() == [[1.0, 1.0], [0.25, 0.25]]
    assert samples.tolist() == [[0.5, 0.5], [0.25, 0.25]]


def test_silent_voice_returns_the_same_samples():
    samples = np.zeros((2, 2), dtype=np.float32)
    assert shim.mix(samples, frames(0.0, 0.0)) is samples


class Base:
    def __init__(self, samples):
        self.samples = samples

    def get_audio_sample(self):
        return self.samples


class FakeTap:
    def __init__(self):
        self.buffer = shim.VoiceBuffer()

    def ensure(self):
        pass


def test_wrapped_read_adds_voice_and_passes_none_through():
    class Audio(Base):
        pass

    tap = FakeTap()
    shim.wrap(Audio, tap)
    assert Audio(None).get_audio_sample() is None
    tap.buffer.feed(frames(0.5))
    out = Audio(np.zeros((1, 2), dtype=np.float32)).get_audio_sample()
    assert out.tolist() == [[0.5, 0.5]]


def test_a_failing_tap_leaves_the_mic_alone():
    class Audio(Base):
        pass

    class Broken(FakeTap):
        def ensure(self):
            raise RuntimeError("no")

    shim.wrap(Audio, Broken())
    samples = np.zeros((1, 2), dtype=np.float32)
    assert Audio(samples).get_audio_sample() is samples


def test_installed_module_wraps_the_sdk_only_when_imported(tmp_path: Path):
    site = tmp_path / "site"
    (site / "reachy_mini/media").mkdir(parents=True)
    (site / "reachy_mini/__init__.py").write_text("")
    (site / "reachy_mini/media/__init__.py").write_text("")
    (site / "reachy_mini/media/audio_base.py").write_text(
        "class AudioBase:\n    def get_audio_sample(self):\n        return None\n"
    )
    app_voice.install(site)
    code = (
        "import sys, site; site.addsitedir(sys.argv[1]);"
        "assert 'numpy' not in sys.modules and 'gi' not in sys.modules;"
        "assert 'reachy_bridge_app_voice' in sys.modules;"
        "import reachy_mini.media.audio_base as a;"
        "assert a.AudioBase.get_audio_sample.__module__ == 'reachy_bridge_app_voice'"
    )
    subprocess.run([sys.executable, "-S", "-c", code, str(site)], check=True)
    app_voice.remove(site)
    assert not list(site.glob("reachy_bridge_app_voice*"))
