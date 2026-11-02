import { describe, expect, test } from "bun:test";
import {
  chipsDisabled,
  expressionError,
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

describe("expressionError", () => {
  test("only a play failure on a live robot shows", () => {
    expect(expressionError(true, true, null)).toBeNull();
    expect(expressionError(true, true, "Robot busy")).toBe("Robot busy");
    expect(expressionError(false, true, "x")).toBeNull();
    expect(expressionError(true, false, "x")).toBeNull();
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
