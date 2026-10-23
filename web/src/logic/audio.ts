// Audio spectrum meter model. Pure: the panel paints a snapshot of it.

export const NBARS = 18;
export const EQ_FLOOR = 4; // settled bar height (%)
export const EQ_SPAN = 88; // full-scale travel above the floor (%)
// The daemon polls at 10 Hz. These per-sample gains reach roughly 75% of a new
// level in 150 ms and decay roughly 90% toward the floor in 500 ms.
const EQ_ATTACK = 0.75;
const EQ_RELEASE = 0.35;
const EQ_RMS_ATTACK = 0.75;
const EQ_RMS_RELEASE = 0.35;
// Quiet hysteresis: enter below 0.02 sustained for 12 events (~1.2 s at 10 Hz),
// wake at 0.04, then settle to the floor and stop DOM writes.
export const EQ_QUIET_ENTER = 0.02;
export const EQ_QUIET_EXIT = 0.04;
export const EQ_QUIET_TICKS = 12;
export const EQ_PEAK_DECAY_DB = 2.4; // ~24 dB/s peak decay at 10 Hz

export interface AudioLevels {
  rms: number;
  low: number;
  mid: number;
  high: number;
  spectrum: number[];
  db: number;
  rateHz: number;
  source: string;
  timestamp: string;
}

const finiteNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));

/**
 * Validate one complete `audio-level` payload. A partial or out-of-contract
 * payload is "unavailable", never a zero-filled spectrum that could look like a
 * real quiet signal.
 */
export function audioLevelsFromPayload(payload: unknown): AudioLevels | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const p = payload as Record<string, unknown>;
  const fields = ["rms", "rms_db", "low", "mid", "high", "sample_rate_hz"];
  if (!fields.every((field) => finiteNumber(p[field]))) return null;
  if (!["rms", "low", "mid", "high"].every((field) => (p[field] as number) >= 0 && (p[field] as number) <= 1)) {
    return null;
  }
  const spectrum = p.spectrum;
  if (
    !Array.isArray(spectrum) ||
    spectrum.length !== NBARS ||
    !spectrum.every((value) => finiteNumber(value) && value >= 0 && value <= 1)
  ) {
    return null;
  }
  const db = p.rms_db as number;
  const rate = p.sample_rate_hz as number;
  if (db < -60 || db > 0 || rate <= 0) return null;
  if (typeof p.source !== "string" || !p.source.trim()) return null;
  if (typeof p.timestamp !== "string" || !p.timestamp.trim()) return null;
  return {
    rms: p.rms as number,
    low: p.low as number,
    mid: p.mid as number,
    high: p.high as number,
    spectrum: spectrum as number[],
    db,
    rateHz: rate,
    source: p.source,
    timestamp: p.timestamp,
  };
}

export function formatSampleRate(rateHz: number): string {
  const khz = rateHz / 1000;
  return `${Number.isInteger(khz) ? khz : khz.toFixed(1)} kHz`;
}

export function formatPeak(peakDb: number): string {
  return `${clamp(peakDb, -60, 0).toFixed(1)} dBFS`;
}

export class EqModel {
  bars: number[] = new Array(NBARS).fill(EQ_FLOOR);
  rms = 0;
  quiet = true;
  settled = true;
  peakDb: number | null = null;
  source: string | null = null;
  rateHz: number | null = null;
  private quietTicks = 0;

  constructor(private readonly reduceMotion = false) {}

  reset(): void {
    this.quietTicks = 0;
    this.quiet = true;
    this.peakDb = null;
    this.source = null;
    this.rateHz = null;
    this.bars.fill(EQ_FLOOR);
    this.rms = 0;
    this.settled = true;
  }

  /**
   * Advance by one valid sample. Every 10 Hz sample advances hysteresis,
   * envelope and peak decay, even when reduced motion skips its paint.
   */
  advance(levels: AudioLevels): void {
    this.source = levels.source;
    this.rateHz = levels.rateHz;
    this.peakDb = this.peakDb == null ? levels.db : Math.max(levels.db, this.peakDb - EQ_PEAK_DECAY_DB);

    if (levels.rms >= EQ_QUIET_EXIT) this.quietTicks = 0;
    else if (levels.rms < EQ_QUIET_ENTER) this.quietTicks += 1;
    this.quiet = this.quietTicks >= EQ_QUIET_TICKS;

    // Reduced motion snaps the model, but not its clock: the floor transition
    // happens on the 12th input tick, not the 12th paint.
    if (this.quiet && this.reduceMotion) {
      this.bars.fill(EQ_FLOOR);
      this.rms = 0;
      this.settled = true;
      return;
    }
    // A quiet model already at its floor needs no envelope work.
    if (this.quiet && this.settled) return;

    this.settled = false;
    let maxDelta = 0;
    this.bars.forEach((value, index) => {
      // Each measured FFT bin drives exactly one bar: six low, six mid, six high.
      const env = this.quiet ? 0 : clamp(levels.spectrum[index] ?? 0, 0, 1);
      const target = EQ_FLOOR + EQ_SPAN * env;
      if (this.reduceMotion) this.bars[index] = target;
      else this.bars[index] = value + (target - value) * (target > value ? EQ_ATTACK : EQ_RELEASE);
      maxDelta = Math.max(maxDelta, Math.abs(this.bars[index] - target));
    });

    const rmsTarget = this.quiet ? 0 : levels.rms * 100;
    if (this.reduceMotion) this.rms = rmsTarget;
    else this.rms += (rmsTarget - this.rms) * (rmsTarget > this.rms ? EQ_RMS_ATTACK : EQ_RMS_RELEASE);

    if (this.quiet && maxDelta < 0.5 && this.rms < 0.5) {
      this.bars.fill(EQ_FLOOR);
      this.rms = 0;
      this.settled = true;
    }
  }
}
