import { describe, expect, test } from "bun:test";
import { simView, syncTarget } from "./sim.ts";

describe("syncTarget", () => {
  test("adopts the first target and reloads on a switch made elsewhere", () => {
    expect(syncTarget(null, "robot", false)).toBe("adopt");
    expect(syncTarget("robot", "robot", false)).toBe("keep");
    expect(syncTarget("robot", "sim", false)).toBe("reload");
  });

  test("waits out its own switch and old controllers", () => {
    expect(syncTarget("robot", "sim", true)).toBe("keep");
    expect(syncTarget("robot", undefined, false)).toBe("keep");
  });

  test("a poll from before its own switch doesn't reload", () => {
    expect(syncTarget("sim", "robot", false, "sim")).toBe("keep");
    expect(syncTarget("sim", "sim", false, "sim")).toBe("keep");
  });
});

describe("simView", () => {
  test("entering needs the robot off", () => {
    expect(simView("robot", false, false).disabled).toBe(true);
    expect(simView("robot", false, true).disabled).toBe(false);
  });

  test("leaving is always allowed", () => {
    expect(simView("sim", false, false)).toMatchObject({ action: "Back to the robot", disabled: false });
    expect(simView("sim", true, true).disabled).toBe(true);
  });
});
