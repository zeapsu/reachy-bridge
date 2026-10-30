import { describe, expect, test } from "bun:test";
import type { ControlView } from "../api/control.ts";
import { controlNotice, controlState, holderLabel, showStrip, type ControlState } from "./control.ts";

const base = (over: Partial<ControlView> = {}): ControlView => ({
  admin_mode: true,
  now: 1000,
  holder: null,
  you: { admin: false, person: "Sam", can_take: true },
  ...over,
});

const holder = { person: "Sam", device: "phone", admin: false, since: 900 };

describe("controlState", () => {
  test("is off outside admin mode", () => {
    expect(controlState({ admin_mode: false })).toEqual({ kind: "off" });
    expect(controlState(null)).toEqual({ kind: "off" });
  });
  test("is mine while this browser holds control", () => {
    expect(controlState(base({ holder: { ...holder, you: true } }))).toEqual({ kind: "mine" });
  });
  test("names someone else in control; only the admin can take over", () => {
    const other = { ...holder, person: "Kim", device: "tablet", you: false };
    const sam = base({ holder: other, you: { admin: false, person: "Sam", can_take: false } });
    expect(controlState(sam)).toEqual({ kind: "other", person: "Kim", canTakeOver: false });
    const admin = base({ holder: other, you: { admin: true, person: "Andry", can_take: true } });
    expect(controlState(admin)).toEqual({ kind: "other", person: "Kim", canTakeOver: true });
  });
  test("is free to take when nobody holds control", () => {
    expect(controlState(base())).toEqual({ kind: "free", canTake: true });
    expect(controlState(base({ you: { admin: false, person: null, can_take: false } }))).toEqual({ kind: "free", canTake: false });
  });
});

describe("control notices, label and strip", () => {
  const off: ControlState = { kind: "off" };
  const mine: ControlState = { kind: "mine" };
  const free: ControlState = { kind: "free", canTake: true };
  const kim: ControlState = { kind: "other", person: "Kim", canTakeOver: false };
  const lee: ControlState = { kind: "other", person: "Lee", canTakeOver: false };
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
  test("says when this person's other device took control", () => {
    const desk: ControlState = { kind: "other", person: "Andry", canTakeOver: true, elsewhere: "desktop" };
    expect(controlNotice(mine, desk)).toBe("You took control on desktop");
    expect(controlNotice(free, desk)).toBe("You took control on desktop");
    const view = base({ holder: { ...holder, device: "laptop", you: false } });
    expect(controlState(view)).toMatchObject({ kind: "other", elsewhere: "laptop", canTakeOver: true });
  });
  test("stays quiet while nothing changes", () => {
    expect(controlNotice(mine, mine)).toBeNull();
    expect(controlNotice(kim, kim)).toBeNull();
    expect(controlNotice(free, free)).toBeNull();
  });
  test("labels the holder and shows the strip only when a tap can't just take control", () => {
    expect(holderLabel(mine)).toBe("you");
    expect(holderLabel(kim)).toBe("Kim");
    expect(holderLabel(free)).toBeNull();
    expect(showStrip(mine)).toBe(false);
    expect(showStrip(free)).toBe(false);
    expect(showStrip(off)).toBe(false);
    expect(showStrip(kim)).toBe(true);
    expect(showStrip({ kind: "free", canTake: false })).toBe(true);
  });
});
