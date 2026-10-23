import { describe, expect, test } from "bun:test";
import { STATS_IDLE, gaugeOffset, statsDisplay } from "./stats.ts";

describe("statsDisplay", () => {
  test("full sample", () => {
    const d = statsDisplay({
      cpu_percent: 42.4,
      gpu_percent: 7,
      cpu_temp_c: 45,
      memory_used_mb: 2048,
      memory_total_mb: 8192,
      alsa_card: 2,
      alsa_pcm_volume: 80,
      pipewire_sink_volume: 0.5,
    });
    expect(d.cpu).toEqual({ percent: 42.4, text: "42", suffix: "%" });
    expect(d.temp).toEqual({ percent: 50, text: "45", suffix: "°C" });
    expect(d.mem).toBe("2.0 / 8.0 GB");
    expect(d.memWidth).toBe("25%");
    expect(d.host).toBe("jetson stats");
    expect(d.card).toBe("card 2 · PCM,1 80%");
    expect(d.sink).toBe("0.50");
  });
  test("missing values read as unknown, and temperature falls back to the GPU", () => {
    const d = statsDisplay({ cpu_percent: null, cpu_temp_c: null, gpu_temp_c: 60, alsa_card: null });
    expect(d.cpu).toEqual({ percent: null, text: "—", suffix: "" });
    expect(d.temp.text).toBe("60");
    expect(d.mem).toBe("— / — GB");
    expect(d.memWidth).toBe("0");
    expect(d.card).toBe("not found");
    expect(d.sink).toBe("not found");
  });
  test("idle readouts", () => {
    expect(STATS_IDLE.host).toBe("unavailable");
    expect(STATS_IDLE.card).toBe("—");
  });
});

test("gauge offset clamps to 0..100%", () => {
  expect(gaugeOffset(null, 100)).toBe(100);
  expect(gaugeOffset(50, 100)).toBe(50);
  expect(gaugeOffset(150, 100)).toBe(0);
  expect(gaugeOffset(-5, 100)).toBe(100);
});
