"""Behavior tests for the real microphone spectrum visualizer (issue #14).

The visualizer lives inside main.js's IIFE, so these tests extract the
self-contained "Audio spectrum visualizer" section and execute it under Node
with a minimal write-counting DOM stub — the same textual-extraction approach
test_controller.py already uses for pollAppsStatus. Skipped when node is not
on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from reachy_controller import main as main_module

_SECTION_START = "// ── Audio spectrum visualizer"
_SECTION_END = "function syncSlider"

_HARNESS = """
"use strict";
const reduceMotion = __REDUCE__;
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const finite = (value) => {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};
const writes = { height: 0, width: 0, text: 0, cls: 0 };
function makeEl(id) {
  const el = { id, children: [], classes: new Set(), style: {}, _text: "" };
  el.appendChild = (child) => el.children.push(child);
  el.classList = {
    toggle: (name, force) => {
      writes.cls += 1;
      if (force) el.classes.add(name);
      else el.classes.delete(name);
    },
  };
  let height = "";
  let width = "";
  Object.defineProperty(el.style, "height", {
    get: () => height,
    set: (value) => { writes.height += 1; height = value; },
  });
  Object.defineProperty(el.style, "width", {
    get: () => width,
    set: (value) => { writes.width += 1; width = value; },
  });
  Object.defineProperty(el, "textContent", {
    get: () => el._text,
    set: (value) => { writes.text += 1; el._text = value; },
  });
  return el;
}
const els = {};
["eq", "eq-rms", "eq-peak", "eq-input", "eq-rate"].forEach((id) => { els[id] = makeEl(id); });
const $ = (id) => els[id];
const document = { createElement: () => makeEl("bar") };

__SECTION__

const results = {};
const BASE = { sample_rate_hz: 16000, source: "daemon mic tap", timestamp: "2026-07-16T08:00:00+00:00" };
const LOW = { ...BASE, rms: 0.9, rms_db: -8.2, low: 0.94, mid: 0, high: 0 };
const MID = { ...BASE, rms: 0.9, rms_db: -8.2, low: 0, mid: 0.94, high: 0 };
const HIGH = { ...BASE, rms: 0.9, rms_db: -8.2, low: 0, mid: 0, high: 0.94 };
const SILENCE = { ...BASE, rms: 0, rms_db: -60, low: 0, mid: 0, high: 0 };
const feed = (payload, count) => {
  for (let i = 0; i < count; i += 1) {
    const levels = audioLevelsFromPayload(payload);
    if (levels) renderAudioLevel(levels);
  }
};
const groupMeans = () => {
  const heights = bars.map((bar) => parseFloat(bar.style.height));
  const mean = (from, to) => heights.slice(from, to).reduce((sum, v) => sum + v, 0) / (to - from);
  return [mean(0, 12), mean(12, 24), mean(24, 36)];
};
const snapshotWrites = () => ({ ...writes });
const writesDelta = (before) =>
  writes.height - before.height + writes.width - before.width + writes.cls - before.cls + writes.text - before.text;

// Boot/offline: settle to the floor exactly once.
renderEqualizer(null, "no audio signal");
results.boot = {
  peak: els["eq-peak"].textContent,
  idle: els.eq.classes.has("idle"),
  heights: [...new Set(bars.map((bar) => bar.style.height))],
  rms: els["eq-rms"].style.width,
};
const afterBoot = snapshotWrites();
renderEqualizer(null, "no audio signal");
renderEqualizer(null, "no audio signal");
results.boot.repeatDelta = writesDelta(afterBoot);

// Band discrimination: each synthetic tone raises its own third.
feed(LOW, 24);
results.low = {
  means: groupMeans(),
  peak: els["eq-peak"].textContent,
  rate: els["eq-rate"].textContent,
  input: els["eq-input"].textContent,
  idle: els.eq.classes.has("idle"),
  rmsWidth: parseFloat(els["eq-rms"].style.width),
};
feed(MID, 24);
results.mid = { means: groupMeans() };
feed(HIGH, 24);
results.high = { means: groupMeans() };

// Loud vs quiet: a weaker mid tone renders a lower hump and narrower ribbon.
feed({ ...MID, rms: 0.3, mid: 0.3 }, 24);
results.midSoft = { means: groupMeans(), rmsWidth: parseFloat(els["eq-rms"].style.width) };
// dBFS clamping of the peak readout remains defensive for already validated
// levels delivered by a trusted caller.
renderEqualizer({ rms: 0.9, low: 0, mid: 0.94, high: 0, db: 3.5, rateHz: 16000, source: "daemon mic tap" });
results.dbHigh = els["eq-peak"].textContent;
for (let i = 0; i < 80; i += 1) {
  renderEqualizer({ rms: 0.9, low: 0, mid: 0.94, high: 0, db: -75, rateHz: 16000, source: "daemon mic tap" });
}
results.dbLow = els["eq-peak"].textContent;

// Quiet hysteresis: settle to the floor, then zero DOM writes per tick.
feed(SILENCE, 60);
results.quiet = {
  idle: els.eq.classes.has("idle"),
  heights: [...new Set(bars.map((bar) => bar.style.height))],
  rms: els["eq-rms"].style.width,
  peak: els["eq-peak"].textContent,
};
const settled = snapshotWrites();
feed(SILENCE, 40);
results.quiet.repeatDelta = writesDelta(settled);

// Offline while settled: status copy updates, no bar churn.
const beforeOffline = snapshotWrites();
renderEqualizer(null, "audio stream reconnecting");
results.offline = {
  peak: els["eq-peak"].textContent,
  heightDelta: writes.height - beforeOffline.height,
  idle: els.eq.classes.has("idle"),
};

// Reconnect: live events wake the meter from the settled offline floor.
feed(MID, 6);
results.reconnect = {
  means: groupMeans(),
  idle: els.eq.classes.has("idle"),
  peak: els["eq-peak"].textContent,
};

// Malformed and incomplete payload safety.
results.malformed = [
  audioLevelsFromPayload(null),
  audioLevelsFromPayload("garbage"),
  audioLevelsFromPayload(42),
  audioLevelsFromPayload({}),
  audioLevelsFromPayload({ ...LOW, rms: "0.9" }),
  audioLevelsFromPayload({ speech_detected: true }),
].map((value) => value === null);
results.invalidContract = [
  { ...LOW, rms: true },
  { ...LOW, rms_db: "-8.2" },
  { ...LOW, low: NaN },
  { ...LOW, mid: null },
  { ...LOW, high: "0" },
  { ...LOW, sample_rate_hz: true },
  { ...LOW, source: " " },
  { ...LOW, timestamp: null },
  { ...LOW, rms: 1.1 },
].map((payload) => audioLevelsFromPayload(payload) === null);
results.complete = audioLevelsFromPayload(LOW) !== null;
results.outOfRange = audioLevelsFromPayload({
  ...LOW, rms: 7, low: -3, mid: 0.5, high: 99, rms_db: -12.5, sample_rate_hz: 48000,
}) === null;
feed(SILENCE, 60);
const beforeGarbage = snapshotWrites();
[null, "garbage", {}, { rms: "abc" }].forEach((payload) => {
  const levels = audioLevelsFromPayload(payload);
  if (levels) renderEqualizer(levels);
});
results.malformedWriteDelta = writesDelta(beforeGarbage);
// Ballistics: 100 ms samples, one rising sample then five release samples.
renderAudioUnavailable("no audio signal");
feed(SILENCE, 60);
feed(LOW, 1);
const centerIdx = 5;
const afterRise = eqState[centerIdx];
feed({ ...SILENCE, rms: 0.05 }, 5); // five 100 ms samples = 500 ms
const afterRelease = eqState[centerIdx];
results.ballistics = {
  floor: EQ_FLOOR,
  afterRise,
  afterRelease,
  attackSamples: 1,
  releaseSamples: 5,
  target: EQ_FLOOR + EQ_SPAN * Math.min(1, 0.94 * eqWeights[centerIdx][0]),
};

// Determinism under repetition: two identical events.
feed(LOW, 1);
const h1 = bars[centerIdx].style.height;
feed(LOW, 1);
const h2 = bars[centerIdx].style.height;
results.snap = { h1, h2 };

renderAudioUnavailable("no audio signal");
const cadencePayload = audioLevelsFromPayload(MID);
results.cadence = Array.from({ length: 10 }, () => renderAudioLevel(cadencePayload));

console.log(JSON.stringify(results));
"""


def _extract_visualizer_section() -> str:
    script = (main_module.STATIC_DIR / "main.js").read_text(encoding="utf-8")
    start = script.index(_SECTION_START)
    end = script.index(_SECTION_END, start)
    return script[start:end]


def _run_harness(reduce_motion: bool) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node executable not available")
    script = _HARNESS.replace("__SECTION__", _extract_visualizer_section()).replace(
        "__REDUCE__", "true" if reduce_motion else "false"
    )
    proc = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def viz() -> dict:
    return _run_harness(reduce_motion=False)


@pytest.fixture(scope="module")
def viz_reduced() -> dict:
    return _run_harness(reduce_motion=True)


def test_low_mid_high_payloads_move_matching_band_groups(viz: dict) -> None:
    low, mid, high = viz["low"]["means"], viz["mid"]["means"], viz["high"]["means"]
    assert low[0] > 2 * low[1] and low[0] > 4 * low[2]
    assert mid[1] > 1.5 * mid[0] and mid[1] > 1.5 * mid[2]
    assert high[2] > 2 * high[1] and high[2] > 4 * high[0]
    # No static shape: the dominant group is well above the floor each time.
    for means, group in ((low, 0), (mid, 1), (high, 2)):
        assert means[group] > 30


def test_loudness_scales_level_and_ribbon(viz: dict) -> None:
    assert viz["midSoft"]["means"][1] < viz["mid"]["means"][1]
    assert viz["midSoft"]["rmsWidth"] < viz["low"]["rmsWidth"]


def test_peak_db_rate_and_source_render_from_payload(viz: dict) -> None:
    assert viz["low"]["peak"] == "-8.2 dBFS"
    assert viz["low"]["rate"] == "16 kHz"
    assert viz["low"]["input"] == "daemon mic tap"
    assert viz["low"]["idle"] is False


def test_peak_db_clamps_to_dbfs_range(viz: dict) -> None:
    assert viz["dbHigh"] == "0.0 dBFS"
    assert viz["dbLow"] == "-60.0 dBFS"


def test_quiet_settles_to_floor_then_zero_dom_writes(viz: dict) -> None:
    assert viz["quiet"]["idle"] is True
    assert viz["quiet"]["heights"] == ["4.0%"]
    assert viz["quiet"]["rms"] == "0.0%"
    assert viz["quiet"]["repeatDelta"] == 0


def test_offline_settles_once_and_updates_status(viz: dict) -> None:
    assert viz["boot"]["peak"] == "no audio signal"
    assert viz["boot"]["idle"] is True
    assert viz["boot"]["heights"] == ["4.0%"]
    assert viz["boot"]["rms"] == "0.0%"
    assert viz["boot"]["repeatDelta"] == 0
    assert viz["offline"]["peak"] == "audio stream reconnecting"
    assert viz["offline"]["heightDelta"] == 0
    assert viz["offline"]["idle"] is True


def test_reconnect_wakes_meter_and_restores_db_readout(viz: dict) -> None:
    assert viz["reconnect"]["idle"] is False
    means = viz["reconnect"]["means"]
    assert means[1] > means[0] and means[1] > means[2]
    assert means[1] > 20
    assert viz["reconnect"]["peak"].endswith(" dBFS")


def test_malformed_payloads_are_rejected_without_fabricated_bands(viz: dict) -> None:
    assert viz["malformed"] == [True] * 6
    assert viz["invalidContract"] == [True] * 9
    assert viz["complete"] is True
    assert viz["outOfRange"] is True
    assert viz["malformedWriteDelta"] == 0


def test_ballistics_match_100ms_transport_samples(viz: dict) -> None:
    ballistics = viz["ballistics"]
    assert ballistics["attackSamples"] == 1
    assert ballistics["releaseSamples"] == 5
    rise = (ballistics["afterRise"] - ballistics["floor"]) / (
        ballistics["target"] - ballistics["floor"]
    )
    remaining = (ballistics["afterRelease"] - ballistics["floor"]) / (
        ballistics["afterRise"] - ballistics["floor"]
    )
    # One 100 ms sample uses ~0.75 attack; five samples cover the documented
    # ~500 ms release and leave about (1 - .35)^5 of the level.
    assert rise == pytest.approx(0.75, abs=0.03)
    assert remaining == pytest.approx((1 - 0.35) ** 5, abs=0.04)

def test_animated_mode_renders_each_audio_event(viz: dict) -> None:
    assert viz["cadence"] == [True] * 10
def test_animated_mode_interpolates_between_events(viz: dict) -> None:
    assert viz["snap"]["h1"] != viz["snap"]["h2"]


def test_reduced_motion_throttles_to_two_hz_and_snaps_to_truth(
    viz_reduced: dict,
) -> None:
    ballistics = viz_reduced["ballistics"]
    assert ballistics["afterRise"] == pytest.approx(ballistics["target"])
    assert ballistics["afterRelease"] == pytest.approx(ballistics["floor"])
    assert viz_reduced["snap"]["h1"] == viz_reduced["snap"]["h2"]
    assert viz_reduced["cadence"] == [True, False, False, False, False, True, False, False, False, False]
    assert viz_reduced["quiet"]["repeatDelta"] == 0
    assert viz_reduced["low"]["peak"] == "-8.2 dBFS"
    low = viz_reduced["low"]["means"]
    assert low[0] > 2 * low[1]


def test_static_audio_visualizer_wiring() -> None:
    static_dir = main_module.STATIC_DIR
    index = (static_dir / "index.html").read_text(encoding="utf-8")
    styles = (static_dir / "style.css").read_text(encoding="utf-8")
    script = (static_dir / "main.js").read_text(encoding="utf-8")

    assert 'id="eq-rms"' in index
    assert 'id="eq-input"' in index
    assert 'id="eq-rate"' in index
    assert 'class="eq-bands"' in index
    assert "--eq-c" in styles
    assert ".eq-rms" in styles
    assert ".eq-bands" in styles

    # The audio SSE handler consumes the real spectrum payload, not the old
    # speech_detected boolean.
    start = script.index('bindEventSource("/api/audio/levels-stream"')
    segment = script[start : script.index("});", start)]
    assert "audioLevelsFromPayload" in segment
    assert "speech_detected" not in segment
    assert 'setAudioStatus("listening")' in segment
    assert 'setAudioStatus("unavailable")' in segment
    assert 'setAudioStatus("reconnecting")' in segment
    daemon_start = script.index("function setDaemonOnline")
    daemon_end = script.index("function setControlsEnabled", daemon_start)
    assert "mic-state" not in script[daemon_start:daemon_end]
