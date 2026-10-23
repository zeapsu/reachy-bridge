import { describe, expect, test } from "bun:test";
import {
  EQ_FLOOR,
  EQ_PEAK_DECAY_DB,
  EQ_QUIET_TICKS,
  EqModel,
  NBARS,
  audioLevelsFromPayload,
  formatPeak,
  formatSampleRate,
  type AudioLevels,
} from "./audio.ts";

const payload = (over: Record<string, unknown> = {}) => ({
  rms: 0.3,
  rms_db: -12,
  low: 0.4,
  mid: 0.3,
  high: 0.2,
  spectrum: new Array(NBARS).fill(0.5),
  sample_rate_hz: 16000,
  source: "XVF3800",
  timestamp: "2026-01-01T00:00:00Z",
  ...over,
});

describe("audioLevelsFromPayload", () => {
  test("accepts a complete payload", () => {
    expect(audioLevelsFromPayload(payload())).toMatchObject({ rms: 0.3, db: -12, rateHz: 16000, source: "XVF3800" });
  });
  test("rejects partial or out-of-contract payloads", () => {
    expect(audioLevelsFromPayload(null)).toBeNull();
    expect(audioLevelsFromPayload([])).toBeNull();
    expect(audioLevelsFromPayload(payload({ rms: undefined }))).toBeNull();
    expect(audioLevelsFromPayload(payload({ rms: 1.5 }))).toBeNull();
    expect(audioLevelsFromPayload(payload({ rms_db: 3 }))).toBeNull();
    expect(audioLevelsFromPayload(payload({ rms_db: -61 }))).toBeNull();
    expect(audioLevelsFromPayload(payload({ sample_rate_hz: 0 }))).toBeNull();
    expect(audioLevelsFromPayload(payload({ spectrum: new Array(17).fill(0) }))).toBeNull();
    expect(audioLevelsFromPayload(payload({ spectrum: [...new Array(17).fill(0), 2] }))).toBeNull();
    expect(audioLevelsFromPayload(payload({ source: " " }))).toBeNull();
    expect(audioLevelsFromPayload(payload({ timestamp: 5 }))).toBeNull();
  });
});

test("formatting", () => {
  expect(formatSampleRate(16000)).toBe("16 kHz");
  expect(formatSampleRate(44100)).toBe("44.1 kHz");
  expect(formatPeak(-12.34)).toBe("-12.3 dBFS");
  expect(formatPeak(-80)).toBe("-60.0 dBFS");
});

describe("EqModel", () => {
  const levels = (rms: number, db = -20, spectrum = 0.5): AudioLevels =>
    audioLevelsFromPayload(payload({ rms, rms_db: db, spectrum: new Array(NBARS).fill(spectrum) }))!;

  test("starts settled at the floor", () => {
    const m = new EqModel();
    expect(m.quiet).toBe(true);
    expect(m.bars.every((b) => b === EQ_FLOOR)).toBe(true);
  });

  test("attacks toward a loud level", () => {
    const m = new EqModel();
    m.advance(levels(0.5));
    expect(m.quiet).toBe(false);
    expect(m.bars[0]).toBeCloseTo(EQ_FLOOR + 0.75 * 88 * 0.5);
  });

  test("quiet needs 12 sustained quiet samples, and wakes at 0.04", () => {
    const m = new EqModel();
    m.advance(levels(0.5));
    for (let i = 0; i < EQ_QUIET_TICKS - 1; i += 1) m.advance(levels(0.01));
    expect(m.quiet).toBe(false);
    m.advance(levels(0.01));
    expect(m.quiet).toBe(true);
    // 0.03 sits in the hysteresis band: stays quiet.
    m.advance(levels(0.03));
    expect(m.quiet).toBe(true);
    m.advance(levels(0.04));
    expect(m.quiet).toBe(false);
  });

  test("quiet eases to the floor and settles", () => {
    const m = new EqModel();
    m.advance(levels(0.5));
    for (let i = 0; i < 40; i += 1) m.advance(levels(0.0));
    expect(m.settled).toBe(true);
    expect(m.bars.every((b) => b === EQ_FLOOR)).toBe(true);
    expect(m.rms).toBe(0);
  });

  test("reduced motion snaps on the 12th quiet tick", () => {
    const m = new EqModel(true);
    m.advance(levels(0.5));
    expect(m.bars[0]).toBe(EQ_FLOOR + 88 * 0.5);
    for (let i = 0; i < EQ_QUIET_TICKS; i += 1) m.advance(levels(0.0));
    expect(m.settled).toBe(true);
    expect(m.bars[0]).toBe(EQ_FLOOR);
  });

  test("peak holds the max and decays 2.4 dB per sample", () => {
    const m = new EqModel();
    m.advance(levels(0.5, -10));
    m.advance(levels(0.5, -40));
    expect(m.peakDb).toBeCloseTo(-10 - EQ_PEAK_DECAY_DB);
    m.advance(levels(0.5, -5));
    expect(m.peakDb).toBe(-5);
  });

  test("reset clears source, rate and peak", () => {
    const m = new EqModel();
    m.advance(levels(0.5));
    m.reset();
    expect(m).toMatchObject({ peakDb: null, source: null, rateHz: null, quiet: true, settled: true, rms: 0 });
  });
});
