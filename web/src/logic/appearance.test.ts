import { describe, expect, test } from "bun:test";
import { DIM_DEFAULT, DIM_MAX, clampDim, fitWithin } from "./appearance.ts";

describe("appearance", () => {
  test("dim clamps to 0..max, unreadable is the default", () => {
    expect(clampDim("40")).toBe(40);
    expect(clampDim(-5)).toBe(0);
    expect(clampDim(500)).toBe(DIM_MAX);
    expect(clampDim(null)).toBe(DIM_DEFAULT);
    expect(clampDim("x")).toBe(DIM_DEFAULT);
  });
  test("photos shrink to fit, never grow", () => {
    expect(fitWithin(4000, 3000, 1920)).toEqual({ w: 1920, h: 1440 });
    expect(fitWithin(800, 600, 1920)).toEqual({ w: 800, h: 600 });
  });
});
