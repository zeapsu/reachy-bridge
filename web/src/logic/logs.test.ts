import { describe, expect, test } from "bun:test";
import { LOG_CAP, appendCapped, emptyLogMessage, logStatusDisplay, parseLogSources, pickLogSource } from "./logs.ts";

describe("appendCapped", () => {
  test("appends with increasing ids and coerces values", () => {
    const r = appendCapped([], ["a", 1, null], 5);
    expect(r.lines).toEqual([
      { id: 5, text: "a" },
      { id: 6, text: "1" },
      { id: 7, text: "" },
    ]);
    expect(r.nextId).toBe(8);
  });
  test("keeps only the newest cap lines", () => {
    let state = { lines: [] as { id: number; text: string }[], nextId: 0 };
    for (let batch = 0; batch < 7; batch += 1) {
      state = appendCapped(state.lines, Array.from({ length: 100 }, (_, i) => `${batch}-${i}`), state.nextId);
    }
    expect(state.lines.length).toBe(LOG_CAP);
    expect(state.lines[0].text).toBe("2-0");
    expect(state.lines.at(-1)?.text).toBe("6-99");
  });
  test("a single oversized batch is trimmed too", () => {
    const r = appendCapped([], Array.from({ length: 750 }, (_, i) => i), 0, 500);
    expect(r.lines.length).toBe(500);
    expect(r.lines[0].text).toBe("250");
  });
  test("non-array or empty input is a no-op (same array)", () => {
    const lines = [{ id: 1, text: "x" }];
    expect(appendCapped(lines, undefined, 2).lines).toBe(lines);
    expect(appendCapped(lines, [], 2)).toEqual({ lines, nextId: 2 });
  });
});

test("empty-pane message per status", () => {
  expect(emptyLogMessage("offline")).toBe("daemon offline — no output");
  expect(emptyLogMessage("unavailable")).toBe("output isn't captured yet");
  expect(emptyLogMessage("live")).toBe("waiting for output");
});

test("status pill", () => {
  expect(logStatusDisplay("live")).toEqual({ text: "live", dot: "ok" });
  expect(logStatusDisplay("offline")).toEqual({ text: "offline", dot: "error" });
  expect(logStatusDisplay("reconnecting")).toEqual({ text: "reconnecting", dot: "loading" });
  expect(logStatusDisplay("unavailable")).toEqual({ text: "not captured", dot: "" });
  expect(logStatusDisplay("")).toEqual({ text: "idle", dot: "" });
});

test("sources parsing and selection", () => {
  expect(parseLogSources({ sources: [{ id: "controller" }, { id: 3 }, null, { id: "daemon" }] })).toEqual([
    "controller",
    "daemon",
  ]);
  expect(parseLogSources(null)).toEqual([]);
  expect(pickLogSource("daemon", ["controller", "daemon"])).toBe("daemon");
  expect(pickLogSource("gone", ["controller", "daemon"])).toBe("controller");
  expect(pickLogSource("gone", [])).toBe("controller");
});
