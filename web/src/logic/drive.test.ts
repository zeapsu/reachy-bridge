import { describe, expect, test } from "bun:test";
import {
  ANTENNA_PARK,
  CENTRED,
  DEFAULT_LIMITS,
  HOME,
  driveBlocked,
  driveMessage,
  parseLimits,
  stepToward,
  stickPose,
} from "./drive.ts";

const DEG = Math.PI / 180;

describe("stickPose", () => {
  test("centred sticks are home", () => {
    expect(stickPose(CENTRED)).toEqual(HOME);
  });

  test("head pad: right looks right, down looks down", () => {
    const pose = stickPose({ ...CENTRED, head: { x: 1, y: 1 } });
    expect(pose.headYaw).toBeCloseTo(-65 * DEG);
    expect(pose.pitch).toBeCloseTo(40 * DEG);
  });

  test("body pad: right turns right, up raises, down lowers", () => {
    expect(stickPose({ ...CENTRED, body: { x: 1, y: 0 } }).bodyYaw).toBeCloseTo(-160 * DEG);
    expect(stickPose({ ...CENTRED, body: { x: 0, y: -1 } }).z).toBeCloseTo(0.02);
    expect(stickPose({ ...CENTRED, body: { x: 0, y: 1 } }).z).toBeCloseTo(-0.03);
  });

  test("sticks past the edge or not numbers stay in range", () => {
    const pose = stickPose({ head: { x: 5, y: Number.NaN }, body: { x: -9, y: 0 }, antennas: [3, 3] });
    expect(pose.headYaw).toBeCloseTo(-65 * DEG);
    expect(pose.pitch).toBe(0);
    expect(pose.bodyYaw).toBeCloseTo(160 * DEG);
    // Slider order is [left, right]; the pose is daemon order [right, left],
    // and + swings both antennas away from the middle.
    expect(pose.antennas[0]).toBeCloseTo(-90 * DEG);
    expect(pose.antennas[1]).toBeCloseTo(90 * DEG);
  });
});

describe("stepToward", () => {
  test("moves no faster than the speed limit", () => {
    const goal = stickPose({ ...CENTRED, head: { x: 0, y: 1 }, body: { x: 0, y: 1 } });
    const next = stepToward(HOME, goal, 0.1);
    expect(next.pitch).toBeCloseTo(9 * DEG);
    expect(next.z).toBeCloseTo(-0.004);
  });

  test("lands exactly on a goal within one step", () => {
    const goal = { ...HOME, pitch: 1 * DEG };
    expect(stepToward(HOME, goal, 1 / 30)).toEqual(goal);
  });

  test("a long frame gap counts as a short one", () => {
    const goal = { ...HOME, pitch: 40 * DEG };
    expect(stepToward(HOME, goal, 5).pitch).toBeCloseTo(9 * DEG);
  });
});

test("driveMessage sends head yaw as absolute", () => {
  const message = JSON.parse(driveMessage({ ...HOME, headYaw: 0.2, bodyYaw: 0.5 }));
  expect(message).toEqual({
    head: { z: 0, pitch: 0, yaw: 0.7 },
    body_yaw: 0.5,
    antennas: ANTENNA_PARK,
  });
});

test("parseLimits reads the controller's ready message", () => {
  const limits = parseLimits({
    type: "ready",
    limits: {
      head: { pitch: [-0.5, 0.5], yaw_from_body: [-1, 1], z: [-0.01, 0.01] },
      body_yaw: [-2, 2],
      antennas: [-1.2, 1.2],
    },
  });
  expect(limits).toEqual({ pitch: 0.5, headYaw: 1, bodyYaw: 2, z: [-0.01, 0.01], antenna: 1.2 });
  expect(parseLimits({ type: "ready" })).toEqual(DEFAULT_LIMITS);
  expect(parseLimits(null)).toEqual(DEFAULT_LIMITS);
});

test("driveBlocked names the first thing in the way", () => {
  const ok = { daemonOnline: true, motorMode: "enabled", appActive: false, exprBusy: false, controlBusy: false };
  expect(driveBlocked(ok)).toBeNull();
  expect(driveBlocked({ ...ok, daemonOnline: false })).toContain("on");
  expect(driveBlocked({ ...ok, motorMode: "gravity_compensation" })).toContain("Enabled");
  expect(driveBlocked({ ...ok, appActive: true, motorMode: "disabled" })).toContain("app");
  expect(driveBlocked({ ...ok, exprBusy: true })).toContain("move");
  expect(driveBlocked({ ...ok, controlBusy: true })).not.toBeNull();
});
