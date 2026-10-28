import { describe, expect, test } from "bun:test";
import { DIM_DEFAULT, DIM_MAX, clampDim, fitWithin, validPhoto } from "./appearance.ts";

describe("appearance", () => {
  test("dim clamps to 0..max, unreadable is the default", () => {
    expect(clampDim("40")).toBe(40);
    expect(clampDim(-5)).toBe(0);
    expect(clampDim(500)).toBe(DIM_MAX);
    expect(clampDim(null)).toBe(DIM_DEFAULT);
    expect(clampDim("x")).toBe(DIM_DEFAULT);
  });
  test("only this page's own photo data URLs are used", () => {
    expect(validPhoto("data:image/jpeg;base64,/9j/4AAQ+x=")).toBe("data:image/jpeg;base64,/9j/4AAQ+x=");
    expect(validPhoto('data:image/jpeg;base64,abc") , url("https://x')).toBeNull();
    expect(validPhoto("https://example.com/a.jpg")).toBeNull();
    expect(validPhoto(null)).toBeNull();
  });
  test("photos shrink to fit, never grow", () => {
    expect(fitWithin(4000, 3000, 1920)).toEqual({ w: 1920, h: 1440 });
    expect(fitWithin(800, 600, 1920)).toEqual({ w: 800, h: 600 });
  });
});
