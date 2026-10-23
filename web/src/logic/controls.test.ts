import { describe, expect, test } from "bun:test";
import type { Kinematics } from "../api.ts";
import { clampVolume, kinematicsSwitch, kinematicsView } from "./controls.ts";

const kin = (over: Partial<Kinematics> = {}): Kinematics => ({
  engine: "Placo",
  active: "Placo",
  available: ["AnalyticalKinematics", "Placo"],
  ...over,
});

describe("kinematicsView", () => {
  test("unknown kinematics disables everything", () => {
    const v = kinematicsView(null, false, false, false);
    expect(v.buttons.every((b) => b.disabled)).toBe(true);
    expect(v.gravityDisabled).toBe(true);
    expect(v.hint).toBe("Kinematics unavailable.");
  });
  test("uninstalled engines are disabled with a title", () => {
    const v = kinematicsView(kin({ available: ["AnalyticalKinematics"], engine: "AnalyticalKinematics", active: null }), false, false, false);
    const placo = v.buttons.find((b) => b.engine === "Placo")!;
    expect(placo.disabled).toBe(true);
    expect(placo.title).toBe("Not installed on the robot");
    expect(v.buttons.find((b) => b.engine === "AnalyticalKinematics")!.active).toBe(true);
  });
  test("gravity needs Placo running", () => {
    const v = kinematicsView(kin({ engine: "AnalyticalKinematics", active: "AnalyticalKinematics" }), false, false, false);
    expect(v.gravityDisabled).toBe(true);
    expect(v.hint).toBe("Gravity compensation needs Placo kinematics.");
  });
  test("saved engine differs from running: takes effect next power-on", () => {
    expect(kinematicsView(kin({ engine: "AnalyticalKinematics" }), false, false, false).hint).toBe(
      "Takes effect at the next power-on.",
    );
  });
  test("gravity can't be turned on during an app, but can be turned off", () => {
    expect(kinematicsView(kin(), false, true, false).gravityDisabled).toBe(true);
    expect(kinematicsView(kin(), false, true, true).gravityDisabled).toBe(false);
    expect(kinematicsView(kin(), false, true, false).hint).toBe("Gravity compensation is off while an app runs.");
  });
  test("controls disabled (offline / busy) disables all", () => {
    const v = kinematicsView(kin(), true, false, false);
    expect(v.buttons.every((b) => b.disabled)).toBe(true);
    expect(v.gravityDisabled).toBe(true);
  });
  test("Placo running, no app: gravity available, no hint", () => {
    const v = kinematicsView(kin(), false, false, false);
    expect(v.gravityDisabled).toBe(false);
    expect(v.hint).toBe("");
  });
});

describe("kinematicsSwitch", () => {
  test("no-op only when both saved and running engine match", () => {
    expect(kinematicsSwitch(kin(), "Placo")).toEqual({ needed: false, robotOn: true });
    expect(kinematicsSwitch(kin({ active: null }), "Placo")).toEqual({ needed: false, robotOn: false });
    expect(kinematicsSwitch(kin({ active: "AnalyticalKinematics" }), "Placo").needed).toBe(true);
    expect(kinematicsSwitch(kin({ active: null }), "AnalyticalKinematics")).toEqual({ needed: true, robotOn: false });
  });
});

test("volume clamps to 0..100", () => {
  expect(clampVolume(120)).toBe(100);
  expect(clampVolume(-3)).toBe(0);
  expect(clampVolume("42")).toBe(42);
});
