import { describe, expect, test } from "bun:test";
import type { ControlView } from "../api/control.ts";
import { controlState, slotText } from "./control.ts";

const base = (over: Partial<ControlView> = {}): ControlView => ({
  admin_mode: true,
  now: 1000,
  holder: null,
  you: { admin: false, person: "Sam", grant: null, can_take: false, cooldown_until: null },
  grants: [],
  requests: [],
  ...over,
});

const holder = { person: "Sam", device: "phone", admin: false, since: 900 };

describe("controlState", () => {
  test("is off outside admin mode", () => {
    expect(controlState({ admin_mode: false })).toEqual({ kind: "off" });
    expect(controlState(null)).toEqual({ kind: "off" });
  });
  test("warns the holder a minute before the grant ends", () => {
    expect(controlState(base({ holder: { ...holder, until: 1100, you: true } }))).toEqual({ kind: "mine", until: 1100, endingSoon: false });
    expect(controlState(base({ holder: { ...holder, until: 1060, you: true } }))).toEqual({ kind: "mine", until: 1060, endingSoon: true });
    expect(controlState(base({ holder: { ...holder, until: null, you: true } }))).toEqual({ kind: "mine", until: null, endingSoon: false });
  });
  test("shows who else holds control, and lets an admin take over", () => {
    const other = { ...holder, person: "Andry", until: null, you: false };
    expect(controlState(base({ holder: other }))).toEqual({ kind: "other", person: "Andry", until: null, canTakeOver: false });
    const admin = base({ holder: other, you: { admin: true, person: "Andry", grant: null, can_take: true, cooldown_until: null } });
    expect(controlState(admin)).toMatchObject({ kind: "other", canTakeOver: true });
  });
  test("asks, waits, or cools down when nobody holds control", () => {
    expect(controlState(base())).toEqual({ kind: "free", canTake: false });
    expect(controlState(base({ you: { admin: false, person: "Sam", grant: null, can_take: true, cooldown_until: null } }))).toEqual({ kind: "free", canTake: true });
    const ask = { person: "Sam", device: "d1", at: 990, minutes: 30, note: "" };
    expect(controlState(base({ requests: [ask] }))).toEqual({ kind: "asked" });
    expect(controlState(base({ you: { admin: false, person: "Sam", grant: null, can_take: false, cooldown_until: 1600 } }))).toEqual({ kind: "cooldown", until: 1600 });
  });
});

describe("slotText", () => {
  test("reads naturally", () => {
    expect(slotText(15)).toBe("15 min");
    expect(slotText(60)).toBe("1 h");
    expect(slotText(90)).toBe("90 min");
    expect(slotText(null)).toBe("no end");
  });
});
