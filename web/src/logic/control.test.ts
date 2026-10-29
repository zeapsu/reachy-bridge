import { describe, expect, test } from "bun:test";
import type { ControlView } from "../api/control.ts";
import { controlNotice, controlState, holderLabel, showStrip, slotText, type ControlState } from "./control.ts";

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
    expect(controlState(base({ holder: other }))).toEqual({ kind: "other", person: "Andry", until: null, canTakeOver: false, asking: { kind: "can_ask" } });
    const admin = base({ holder: other, you: { admin: true, person: "Andry", grant: null, can_take: true, cooldown_until: null } });
    expect(controlState(admin)).toMatchObject({ kind: "other", canTakeOver: true });
  });
  test("asks, waits, or cools down when nobody holds control", () => {
    expect(controlState(base())).toEqual({ kind: "free", canTake: false, asking: { kind: "can_ask" } });
    const grant = { id: "g", person: "Sam", from: 900, until: null, by: "a" };
    expect(controlState(base({ you: { admin: false, person: "Sam", grant, can_take: true, cooldown_until: null } }))).toEqual({ kind: "free", canTake: true, asking: { kind: "none" } });
    const ask = { person: "Sam", device: "d1", at: 990, minutes: 30, note: "" };
    expect(controlState(base({ requests: [ask] }))).toMatchObject({ asking: { kind: "asked" } });
    expect(controlState(base({ you: { admin: false, person: "Sam", grant: null, can_take: false, cooldown_until: 1600 } }))).toMatchObject({ asking: { kind: "cooldown", until: 1600 } });
    // Asking works while someone else drives.
    const other = { ...holder, person: "Kim", until: null, you: false };
    expect(controlState(base({ holder: other, requests: [ask] }))).toMatchObject({ kind: "other", asking: { kind: "asked" } });
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

describe("control notices, label and strip", () => {
  const asking = { kind: "none" } as const;
  const off: ControlState = { kind: "off" };
  const mine: ControlState = { kind: "mine", until: null, endingSoon: false };
  const free: ControlState = { kind: "free", canTake: true, asking };
  const kim: ControlState = { kind: "other", person: "Kim", until: null, canTakeOver: true, asking };
  const lee: ControlState = { kind: "other", person: "Lee", until: null, canTakeOver: true, asking };
  test("says nothing on first load or outside admin mode", () => {
    expect(controlNotice(off, mine)).toBeNull();
    expect(controlNotice(off, kim)).toBeNull();
    expect(controlNotice(mine, off)).toBeNull();
  });
  test("names each change of holder", () => {
    expect(controlNotice(free, mine)).toBe("You're in control");
    expect(controlNotice(kim, mine)).toBe("You're in control");
    expect(controlNotice(mine, kim)).toBe("Kim took over");
    expect(controlNotice(free, kim)).toBe("Kim took control");
    expect(controlNotice(kim, lee)).toBe("Lee took control");
    expect(controlNotice(mine, free)).toBe("You're no longer in control");
    expect(controlNotice(kim, free)).toBe("Kim let go of control");
  });
  test("stays quiet while nothing changes, and warns once before time ends", () => {
    expect(controlNotice(mine, mine)).toBeNull();
    expect(controlNotice(kim, kim)).toBeNull();
    expect(controlNotice(free, free)).toBeNull();
    const ending: ControlState = { kind: "mine", until: 1060, endingSoon: true };
    expect(controlNotice({ kind: "mine", until: 1060, endingSoon: false }, ending)).toContain("under a minute");
    expect(controlNotice(ending, ending)).toBeNull();
  });
  test("labels the holder and shows the strip only when a tap can't just take control", () => {
    expect(holderLabel(mine)).toBe("you");
    expect(holderLabel(kim)).toBe("Kim");
    expect(holderLabel(free)).toBeNull();
    expect(showStrip(mine)).toBe(false);
    expect(showStrip(free)).toBe(false);
    expect(showStrip(off)).toBe(false);
    expect(showStrip(kim)).toBe(true);
    expect(showStrip({ kind: "free", canTake: false, asking: { kind: "can_ask" } })).toBe(true);
  });
});
