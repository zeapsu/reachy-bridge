import type { SystemStats } from "../api.ts";
import { finite } from "./pose.ts";

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));

export interface GaugeValue {
  percent: number | null;
  text: string;
  suffix: string;
}

export interface StatsDisplay {
  cpu: GaugeValue;
  gpu: GaugeValue;
  temp: GaugeValue;
  mem: string;
  memWidth: string;
  host: string;
  card: string;
  sink: string;
}

const blank: GaugeValue = { percent: null, text: "—", suffix: "" };

/** Readouts while the daemon is offline (the backend stops sampling). */
export const STATS_IDLE: StatsDisplay = {
  cpu: blank,
  gpu: blank,
  temp: blank,
  mem: "— / — GB",
  memWidth: "0",
  host: "unavailable",
  card: "—",
  sink: "—",
};

const percentGauge = (value: number | null): GaugeValue =>
  value == null ? blank : { percent: value, text: value.toFixed(0), suffix: "%" };

export function statsDisplay(stats: SystemStats): StatsDisplay {
  const cpu = finite(stats.cpu_percent);
  const gpu = finite(stats.gpu_percent);
  const temperature = finite(stats.cpu_temp_c) ?? finite(stats.gpu_temp_c);
  const used = finite(stats.memory_used_mb);
  const total = finite(stats.memory_total_mb);
  let mem = "— / — GB";
  let memWidth = "0";
  if (used != null && total) {
    mem = `${(used / 1024).toFixed(1)} / ${(total / 1024).toFixed(1)} GB`;
    memWidth = `${clamp((used / total) * 100, 0, 100).toFixed(0)}%`;
  }
  return {
    cpu: percentGauge(cpu),
    gpu: percentGauge(gpu),
    temp:
      temperature == null
        ? blank
        : { percent: (temperature / 90) * 100, text: temperature.toFixed(0), suffix: "°C" },
    mem,
    memWidth,
    host: "jetson stats",
    card: stats.alsa_card == null ? "not found" : `card ${stats.alsa_card} · PCM,1 ${stats.alsa_pcm_volume ?? "—"}%`,
    sink: stats.pipewire_sink_volume == null ? "not found" : Number(stats.pipewire_sink_volume).toFixed(2),
  };
}

export function gaugeOffset(percent: number | null, circumference: number): number {
  const normalized = percent == null ? 0 : clamp(Number(percent), 0, 100);
  return circumference * (1 - normalized / 100);
}
