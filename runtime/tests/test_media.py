from __future__ import annotations

import math
import sys

import numpy as np
import pytest

from reachy_bridge_runtime.features import media
from reachy_bridge_runtime.features.media import AudioSpectrumTap


SAMPLE_RATE = 16_000
SAMPLES = 4096


def tone(frequency: float, amplitude: float = 0.5) -> np.ndarray:
    t = np.arange(SAMPLES, dtype=np.float32) / SAMPLE_RATE
    return (amplitude * np.sin(2 * math.pi * frequency * t)).astype(np.float32)


@pytest.mark.parametrize(
    ("frequency", "dominant", "expected_bin"),
    [(120.0, "low", 1), (1_000.0, "mid", 8), (5_000.0, "high", 15)],
)
def test_synthetic_tones_dominate_expected_band(frequency: float, dominant: str, expected_bin: int) -> None:
    tap = AudioSpectrumTap(compute_interval=0)

    levels = tap.process_samples(tone(frequency), sample_rate_hz=SAMPLE_RATE, now=0.0)

    assert levels is not None
    spectrum = levels["spectrum"]
    assert isinstance(spectrum, list)
    assert len(spectrum) == 18
    assert all(math.isfinite(value) and 0 <= value <= 1 for value in spectrum)
    assert np.argmax(spectrum) == expected_bin
    assert spectrum[expected_bin] > max(value for index, value in enumerate(spectrum) if index != expected_bin) * 2
    assert levels[dominant] > 0.2
    assert levels[dominant] > max(levels[name] for name in ("low", "mid", "high") if name != dominant) * 2


def test_short_audio_callbacks_fill_rolling_window_before_fft() -> None:
    sample_count = 13 * 160
    times = np.arange(sample_count, dtype=np.float32) / SAMPLE_RATE
    samples = (0.5 * np.sin(2 * math.pi * 1_000 * times)).astype(np.float32)
    tap = AudioSpectrumTap(compute_interval=0.05)

    results = [
        tap.process_samples(
            samples[offset : offset + 160],
            sample_rate_hz=SAMPLE_RATE,
            now=offset / SAMPLE_RATE,
        )
        for offset in range(0, sample_count, 160)
    ]

    assert results[:-1] == [None] * (len(results) - 1)
    levels = results[-1]
    assert levels is not None
    assert len(levels["spectrum"]) == 18
    assert np.argmax(levels["spectrum"]) == 8
    assert tap.compute_count == tap.fft_count == 1
    assert tap.process_samples(samples[:160], sample_rate_hz=SAMPLE_RATE, now=0.13) is None
    assert tap.process_samples(samples[160:320], sample_rate_hz=SAMPLE_RATE, now=0.18) is not None
    assert tap.compute_count == tap.fft_count == 2


def test_amplitude_tracks_rms_monotonically_and_silence_clamps() -> None:
    tap = AudioSpectrumTap(compute_interval=0)

    quiet = tap.process_samples(tone(1_000, 0.1), sample_rate_hz=SAMPLE_RATE, now=0.0)
    loud = tap.process_samples(tone(1_000, 0.8), sample_rate_hz=SAMPLE_RATE, now=1.0)
    silent = tap.process_samples(np.zeros(SAMPLES, dtype=np.float32), sample_rate_hz=SAMPLE_RATE, now=2.0)

    assert quiet is not None and loud is not None and silent is not None
    assert 0 < quiet["rms"] < loud["rms"] <= 1
    assert quiet["rms_db"] < loud["rms_db"] <= 0
    assert silent["rms"] == 0
    assert silent["rms_db"] == -60


def test_empty_and_nonfinite_input_is_safe() -> None:
    tap = AudioSpectrumTap(compute_interval=0)

    assert tap.process_samples(np.array([], dtype=np.float32), sample_rate_hz=SAMPLE_RATE, now=0.0) is None
    assert (
        tap.process_samples(
            np.zeros(AudioSpectrumTap.ANALYSIS_WINDOW_SIZE - 3, dtype=np.float32),
            sample_rate_hz=SAMPLE_RATE,
            now=0.0,
        )
        is None
    )
    levels = tap.process_samples(
        np.array([np.nan, np.inf, -np.inf], dtype=np.float32),
        sample_rate_hz=SAMPLE_RATE,
        now=1.0,
    )

    assert levels is not None
    assert levels["rms"] == 0
    assert levels["spectrum"] == [0.0] * 18
    assert levels["low"] == levels["mid"] == levels["high"] == 0


def test_rate_cap_and_frequency_cache_avoid_repeated_work() -> None:
    tap = AudioSpectrumTap(compute_interval=0.05)
    samples = tone(1_000)

    assert tap.process_samples(samples, sample_rate_hz=SAMPLE_RATE, now=0.0) is not None
    assert tap.process_samples(samples, sample_rate_hz=SAMPLE_RATE, now=0.01) is None
    assert tap.process_samples(samples, sample_rate_hz=SAMPLE_RATE, now=0.051) is not None

    assert tap.compute_count == 2
    assert tap.fft_count == 2
    assert tap.frequency_cache_builds == 1


def test_db_mapping_is_documented_and_bounded() -> None:
    assert AudioSpectrumTap.DB_FLOOR == -60.0
    assert AudioSpectrumTap.DB_CEILING == -6.0
    assert AudioSpectrumTap._db_to_level(-60.0) == 0
    assert AudioSpectrumTap._db_to_level(-6.0) == 1
    assert AudioSpectrumTap._db_to_level(-100.0) == 0
    assert AudioSpectrumTap._db_to_level(0.0) == 1


def test_gstreamer_patch_preserves_webrtc_and_adds_one_analysis_branch(monkeypatch) -> None:
    import types

    created: list[tuple[str, str | None]] = []

    class Element:
        def __init__(self, factory: str, name: str | None = None) -> None:
            self.factory = factory
            self.name = name or factory
            self.downstream: list[Element] = []
            self.upstream: list[Element] = []
            self.properties: dict[str, object] = {}
            self.callbacks: dict[str, object] = {}

        def link(self, other: Element) -> bool:
            if other not in self.downstream:
                self.downstream.append(other)
            if self not in other.upstream:
                other.upstream.append(self)
            return True

        def unlink(self, other: Element) -> None:
            if other in self.downstream:
                self.downstream.remove(other)
                other.upstream.remove(self)

        def set_property(self, key: str, value: object) -> None:
            self.properties[key] = value

        def connect(self, signal: str, callback: object) -> None:
            self.callbacks[signal] = callback

    class Pipeline:
        def __init__(self) -> None:
            self.elements: dict[str, Element] = {}

        def add(self, element: Element) -> bool:
            self.elements[element.name] = element
            return True

        def remove(self, element: Element) -> bool:
            if self.elements.get(element.name) is not element:
                return False
            del self.elements[element.name]
            return True

        def get_by_name(self, name: str) -> Element | None:
            return self.elements.get(name)

    class Factory:
        missing: str | None = None

        @classmethod
        def make(cls, factory: str, name: str | None = None) -> Element | None:
            created.append((factory, name))
            if factory == cls.missing:
                return None
            return Element(factory, name)

    class GstMediaServer:
        _reachy_audio_spectrum_patched = False

        def _configure_audio(self, pipeline: Pipeline, webrtcsink: Element) -> None:
            source = Element("alsasrc", "audiosrc")
            queue = Element("queue", "queue_audiosrc")
            pipeline.add(source)
            pipeline.add(queue)
            source.link(queue)
            queue.link(webrtcsink)

    fake_gst = types.SimpleNamespace(
        ElementFactory=Factory,
        Caps=types.SimpleNamespace(from_string=lambda value: value),
        MapFlags=types.SimpleNamespace(READ=1),
        FlowReturn=types.SimpleNamespace(OK=0),
    )
    fake_gi = types.ModuleType("gi")
    fake_gi.require_version = lambda *_args: None
    fake_repository = types.ModuleType("gi.repository")
    fake_repository.Gst = fake_gst
    fake_media = types.ModuleType("reachy_mini.media.media_server")
    fake_media.GstMediaServer = GstMediaServer

    monkeypatch.setitem(sys.modules, "gi", fake_gi)
    monkeypatch.setitem(sys.modules, "gi.repository", fake_repository)
    monkeypatch.setitem(sys.modules, "reachy_mini.media.media_server", fake_media)
    media.install_audio_spectrum_tap()
    media.install_audio_spectrum_tap()

    pipeline = Pipeline()
    sink = Element("webrtcsink", "webrtcsink")
    server = GstMediaServer()
    server._configure_audio(pipeline, sink)

    source = pipeline.get_by_name("audiosrc")
    queue = pipeline.get_by_name("queue_audiosrc")
    tee = pipeline.get_by_name("audio_capture_tee")
    webrtc_queue = pipeline.get_by_name("queue_audio_webrtc")
    analysis_queue = pipeline.get_by_name("queue_audio_analysis")
    analysis_sink = pipeline.get_by_name("audio_spectrum_sink")
    assert source is not None and queue is not None and tee is not None
    assert webrtc_queue is not None and analysis_queue is not None
    assert queue.downstream == [tee]
    assert tee.downstream == [webrtc_queue, analysis_queue]
    assert webrtc_queue.downstream == [sink]
    assert analysis_queue.downstream == [pipeline.get_by_name("audio_spectrum_convert")]
    assert analysis_sink is not None
    assert analysis_sink.callbacks["new-sample"]
    assert isinstance(server._reachy_audio_spectrum_tap, media.AudioSpectrumTap)
    audio_sources = [
        element
        for element in pipeline.elements.values()
        if element.factory in {"alsasrc", "autoaudiosrc", "pulsesrc", "pipewiresrc"}
    ]
    assert len(audio_sources) == 1
    assert sum(factory == "alsasrc" for factory, _ in created) == 0
    assert sum(factory == "tee" for factory, _ in created) == 1


def test_gstreamer_patch_rolls_back_false_link_and_restores_webrtc(monkeypatch) -> None:
    import types

    class Element:
        def __init__(self, factory: str, name: str | None = None) -> None:
            self.factory = factory
            self.name = name or factory
            self.downstream: list[Element] = []
            self.upstream: list[Element] = []

        def link(self, other: Element) -> bool:
            if self.name == "audio_spectrum_convert":
                return False
            if other not in self.downstream:
                self.downstream.append(other)
            if self not in other.upstream:
                other.upstream.append(self)
            return True

        def unlink(self, other: Element) -> None:
            if other in self.downstream:
                self.downstream.remove(other)
                other.upstream.remove(self)

        def set_property(self, *_args: object) -> None:
            return None

        def connect(self, *_args: object) -> None:
            return None

    class Pipeline:
        def __init__(self) -> None:
            self.elements: dict[str, Element] = {}

        def add(self, element: Element) -> bool:
            self.elements[element.name] = element
            return True

        def remove(self, element: Element) -> bool:
            if self.elements.get(element.name) is not element:
                return False
            del self.elements[element.name]
            return True

        def get_by_name(self, name: str) -> Element | None:
            return self.elements.get(name)

    class Factory:
        @staticmethod
        def make(factory: str, name: str | None = None) -> Element:
            return Element(factory, name)

    class GstMediaServer:
        _reachy_audio_spectrum_patched = False

        def _configure_audio(self, pipeline: Pipeline, sink: Element) -> None:
            source = Element("alsasrc", "audiosrc")
            queue = Element("queue", "queue_audiosrc")
            pipeline.add(source)
            pipeline.add(queue)
            source.link(queue)
            queue.link(sink)

    fake_gst = types.SimpleNamespace(
        ElementFactory=Factory,
        Caps=types.SimpleNamespace(from_string=lambda value: value),
    )
    fake_gi = types.ModuleType("gi")
    fake_gi.require_version = lambda *_args: None
    fake_repository = types.ModuleType("gi.repository")
    fake_repository.Gst = fake_gst
    fake_media = types.ModuleType("reachy_mini.media.media_server")
    fake_media.GstMediaServer = GstMediaServer
    monkeypatch.setitem(sys.modules, "gi", fake_gi)
    monkeypatch.setitem(sys.modules, "gi.repository", fake_repository)
    monkeypatch.setitem(sys.modules, "reachy_mini.media.media_server", fake_media)

    media.install_audio_spectrum_tap()
    pipeline = Pipeline()
    sink = Element("webrtcsink", "webrtcsink")
    server = GstMediaServer()
    server._configure_audio(pipeline, sink)

    queue = pipeline.get_by_name("queue_audiosrc")
    assert queue is not None
    assert queue.downstream == [sink]
    assert getattr(server, "_reachy_audio_spectrum_tap", None) is None
    assert set(pipeline.elements) == {"audiosrc", "queue_audiosrc"}
    assert (
        sum(
            element.factory in {"alsasrc", "autoaudiosrc", "pulsesrc", "pipewiresrc"}
            for element in pipeline.elements.values()
        )
        == 1
    )


def test_gstreamer_patch_keeps_audio_when_optional_tap_element_missing(monkeypatch) -> None:
    import types

    class Element:
        def __init__(self, name: str) -> None:
            self.name = name
            self.linked_to: Element | None = None

        def link(self, other: Element) -> bool:
            self.linked_to = other
            return True

        def unlink(self, other: Element) -> bool:
            if self.linked_to is other:
                self.linked_to = None
                return True
            return False

        def set_property(self, *_args: object) -> None:
            return None

        def connect(self, *_args: object) -> None:
            return None

    class Pipeline:
        def __init__(self) -> None:
            self.elements: dict[str, Element] = {}

        def add(self, element: Element) -> None:
            self.elements[element.name] = element

        def get_by_name(self, name: str) -> Element | None:
            return self.elements.get(name)

    class Factory:
        @staticmethod
        def make(factory: str, name: str | None = None) -> Element | None:
            return None if factory == "appsink" else Element(name or factory)

    class GstMediaServer:
        _reachy_audio_spectrum_patched = False

        def _configure_audio(self, pipeline: Pipeline, sink: Element) -> None:
            queue = Element("queue_audiosrc")
            pipeline.add(queue)
            queue.link(sink)

    fake_gst = types.SimpleNamespace(
        ElementFactory=Factory,
        Caps=types.SimpleNamespace(from_string=lambda value: value),
    )
    fake_gi = types.ModuleType("gi")
    fake_gi.require_version = lambda *_args: None
    fake_repository = types.ModuleType("gi.repository")
    fake_repository.Gst = fake_gst
    fake_media = types.ModuleType("reachy_mini.media.media_server")
    fake_media.GstMediaServer = GstMediaServer
    monkeypatch.setitem(sys.modules, "gi", fake_gi)
    monkeypatch.setitem(sys.modules, "gi.repository", fake_repository)
    monkeypatch.setitem(sys.modules, "reachy_mini.media.media_server", fake_media)

    media.install_audio_spectrum_tap()
    pipeline = Pipeline()
    sink = Element("webrtcsink")
    server = GstMediaServer()
    server._configure_audio(pipeline, sink)

    assert pipeline.get_by_name("queue_audiosrc").linked_to is sink
    assert getattr(server, "_reachy_audio_spectrum_tap", None) is None


def test_runtime_runs_host_setup_then_features_then_the_daemon(monkeypatch) -> None:
    from reachy_bridge_runtime import main as runtime_main
    from reachy_bridge_runtime import platforms, upstream

    calls: list[str] = []
    platform = platforms.Platform(
        name="test",
        tier="tested",
        host_setup=(lambda: calls.append("volume"), lambda: calls.append("asoundrc")),
    )
    monkeypatch.setattr(platforms, "detect", lambda: platform)
    monkeypatch.setattr(runtime_main, "apply", lambda _platform: calls.append("features") or [])
    monkeypatch.setattr(runtime_main, "report_platform", lambda *_args: calls.append("report"))
    monkeypatch.setattr(upstream, "run_daemon", lambda: calls.append("daemon") or 23)
    monkeypatch.setattr(upstream, "installed_version", lambda: "test")
    monkeypatch.setattr(sys, "argv", ["reachy-bridge-runtime"])

    assert runtime_main.main() == 23
    assert calls == ["volume", "asoundrc", "features", "report", "daemon"]


@pytest.mark.asyncio
async def test_audio_levels_route_requires_media_and_fresh_tap_then_reacquires(monkeypatch) -> None:
    import httpx
    import types

    from fastapi import FastAPI

    clock_value = [0.0]
    tap = AudioSpectrumTap(clock=lambda: clock_value[0])
    tap.process_samples(tone(1_000), sample_rate_hz=SAMPLE_RATE, now=0.0)
    daemon = types.SimpleNamespace(
        media_released=False,
        _media_server=types.SimpleNamespace(_reachy_audio_spectrum_tap=tap),
    )
    app = FastAPI()
    app.state.daemon = daemon
    media.install_audio_levels_route(app)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/audio/levels")
        assert response.status_code == 200
        assert response.json()["source"] == "daemon mic tap"

        clock_value[0] = 1.01
        assert (await client.get("/api/audio/levels")).status_code == 503

        clock_value[0] = 1.02
        daemon.media_released = True
        assert (await client.get("/api/audio/levels")).status_code == 503

        daemon.media_released = False
        daemon._media_server._reachy_audio_spectrum_tap = None
        assert (await client.get("/api/audio/levels")).status_code == 503

        reacquired = AudioSpectrumTap(clock=lambda: clock_value[0])
        reacquired.process_samples(tone(5_000), sample_rate_hz=SAMPLE_RATE, now=2.0)
        clock_value[0] = 2.1
        daemon._media_server._reachy_audio_spectrum_tap = reacquired
        assert (await client.get("/api/audio/levels")).status_code == 200
