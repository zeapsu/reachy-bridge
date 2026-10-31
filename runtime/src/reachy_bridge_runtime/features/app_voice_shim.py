"""Let a browser talk to apps (#219). Installed by reachy-bridge.

The bridge copies this file into the apps venv as
``reachy_bridge_app_voice.py`` with a ``.pth`` that imports it. When a process
imports the SDK's audio module, its mic read is wrapped: what the audio gateway
plays into the loopback is added to the samples the app gets.

This happens in the app's process, not in the ALSA mic device, because a mic
device mixing the USB mic and the loopback made every app's own playback lose
audio: the app's one GStreamer pipeline runs on that device's clock, and the
two sides have separate clocks. Here the voice has its own pipeline and only
ever touches the samples, so the mic and the app's audio stay on upstream's
path. Any failure leaves the mic as it was.

Importing it costs next to nothing and imports only the standard library:
it runs in every Python process of the apps venv, most of which never touch
audio. Everything else loads only once the SDK's audio does.
"""

import collections
import importlib.abc
import importlib.util
import logging
import sys
import threading

logger = logging.getLogger("reachy_bridge.app_voice")

TARGET = "reachy_mini.media.audio_base"
DEVICE = "reachy_bridge_app_voice_in"
RATE = 16000
BYTES_PER_FRAME = 8  # F32LE stereo, as the SDK's mic read returns
# Late voice is dropped rather than building up behind the app.
MAX_BYTES = RATE * BYTES_PER_FRAME // 4


class VoiceBuffer:
    """The latest voice audio, oldest dropped first."""

    def __init__(self, limit=MAX_BYTES):
        self._chunks = collections.deque()
        self._size = 0
        self._limit = limit
        self._lock = threading.Lock()

    def feed(self, data):
        with self._lock:
            self._chunks.append(data)
            self._size += len(data)
            while self._size > self._limit:
                self._size -= len(self._chunks.popleft())

    def take(self, size):
        """Up to ``size`` bytes, whole frames only."""
        with self._lock:
            size = min(size, self._size) // BYTES_PER_FRAME * BYTES_PER_FRAME
            out = bytearray()
            while len(out) < size:
                chunk = self._chunks.popleft()
                need = size - len(out)
                out += chunk[:need]
                if len(chunk) > need:
                    self._chunks.appendleft(chunk[need:])
            self._size -= size
            return bytes(out)


def mix(samples, voice):
    """``samples`` (float32, n x 2) with ``voice`` bytes added from the start."""
    import numpy as np

    added = np.frombuffer(voice, dtype=np.float32).reshape(-1, 2)
    if not added.any():
        return samples
    mixed = samples.copy()
    mixed[: len(added)] += added
    return np.clip(mixed, -1.0, 1.0)


class Tap:
    """Reads the loopback on a pipeline of its own."""

    def __init__(self):
        self.buffer = VoiceBuffer()
        self._pipeline = None
        self._lock = threading.Lock()
        self._failed = False

    def ensure(self):
        with self._lock:
            if self._pipeline is not None or self._failed:
                return
            try:
                self._start()
            except Exception as error:  # noqa: BLE001 - the mic works without it
                self._failed = True
                logger.warning("Can't read browser voice for this app: %s", error)

    def _start(self):
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        pipeline = Gst.parse_launch(
            f"alsasrc device={DEVICE} ! audioconvert ! audioresample ! "
            f"audio/x-raw,format=F32LE,channels=2,rate={RATE},layout=interleaved ! "
            "appsink name=sink drop=true max-buffers=10 sync=false emit-signals=true"
        )
        pipeline.get_by_name("sink").connect("new-sample", self._on_sample, Gst.FlowReturn.OK)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            raise RuntimeError(f"couldn't open {DEVICE}")
        self._pipeline = pipeline

    def _on_sample(self, appsink, ok):
        sample = appsink.emit("pull-sample")
        if sample is not None:
            buffer = sample.get_buffer()
            self.buffer.feed(buffer.extract_dup(0, buffer.get_size()))
        return ok


def wrap(cls, tap):
    """Add the voice to ``cls.get_audio_sample``."""
    original = cls.get_audio_sample

    def get_audio_sample(self):
        samples = original(self)
        if samples is None:
            return None
        try:
            tap.ensure()
            voice = tap.buffer.take(len(samples) * BYTES_PER_FRAME)
            return mix(samples, voice) if voice else samples
        except Exception:  # noqa: BLE001 - never break the app's mic
            logger.exception("Couldn't add browser voice")
            return samples

    cls.get_audio_sample = get_audio_sample


class _Finder(importlib.abc.MetaPathFinder):
    """Wraps the SDK's mic read once its module is imported."""

    def find_spec(self, name, path=None, target=None):
        if name != TARGET:
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            return spec
        run = spec.loader.exec_module

        def exec_module(module):
            run(module)
            try:
                wrap(module.AudioBase, Tap())
            except Exception as error:  # noqa: BLE001 - the mic works without it
                logger.warning("Can't add browser voice to this SDK: %s", error)

        spec.loader.exec_module = exec_module
        return spec


if __name__ == "reachy_bridge_app_voice":
    sys.meta_path.insert(0, _Finder())
