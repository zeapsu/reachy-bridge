import { describe, expect, test } from "bun:test";
import {
  HINT_APP_ACTIVE,
  HINT_OFFLINE,
  HINT_RETURNING_HOME,
  chipsDisabled,
  expressionHint,
  parseMoveList,
} from "./expressions.ts";

const ready = { daemonOnline: true, controlBusy: false, appActive: false, exprBusy: false };

describe("chipsDisabled", () => {
  test("enabled only on an idle, online robot no app owns", () => {
    expect(chipsDisabled(ready)).toBe(false);
    expect(chipsDisabled({ ...ready, daemonOnline: false })).toBe(true);
    expect(chipsDisabled({ ...ready, controlBusy: true })).toBe(true);
    expect(chipsDisabled({ ...ready, appActive: true })).toBe(true);
    expect(chipsDisabled({ ...ready, exprBusy: true })).toBe(true);
  });
});

describe("expressionHint", () => {
  const base = { daemonOnline: true, loaded: true, appActive: false, busy: null, error: null } as const;
  test("hidden when idle", () => {
    expect(expressionHint(base)).toEqual({ text: "", hidden: true, error: false });
  });
  test("offline (or lists not loaded yet) explains why chips are dim", () => {
    expect(expressionHint({ ...base, daemonOnline: false, error: "x" }).text).toBe(HINT_OFFLINE);
    expect(expressionHint({ ...base, loaded: false }).text).toBe(HINT_OFFLINE);
  });
  test("app ownership pauses expressions (#66)", () => {
    expect(expressionHint({ ...base, appActive: true })).toEqual({ text: HINT_APP_ACTIVE, hidden: false, error: false });
  });
  test("a playing move hides the hint even if an app starts mid-move", () => {
    expect(expressionHint({ ...base, appActive: true, busy: "play" }).hidden).toBe(true);
  });
  test("return-home after motor enable says so (#67)", () => {
    expect(expressionHint({ ...base, busy: "returning" }).text).toBe(HINT_RETURNING_HOME);
  });
  test("play failure shows the backend's reason as an error", () => {
    expect(expressionHint({ ...base, error: "Robot is busy with another move." })).toEqual({
      text: "Robot is busy with another move.",
      hidden: false,
      error: true,
    });
  });
});

describe("parseMoveList", () => {
  test("accepts an array or a wrapped list", () => {
    expect(parseMoveList(["a", "b"])).toEqual(["a", "b"]);
    expect(parseMoveList({ moves: ["a"] })).toEqual(["a"]);
    expect(parseMoveList({ names: ["n"] })).toEqual(["n"]);
    expect(parseMoveList({ items: ["i"] })).toEqual(["i"]);
  });
  test("anything else is empty", () => {
    expect(parseMoveList(null)).toEqual([]);
    expect(parseMoveList({ moves: "x" })).toEqual([]);
    expect(parseMoveList("text")).toEqual([]);
  });
});
