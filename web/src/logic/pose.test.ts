import { describe, expect, test } from "bun:test";
import { antennaValues, doaDegrees, poseFrame, poseValues } from "./pose.ts";

describe("pose parsing", () => {
  test("head pose: last three of an array, or roll/pitch/yaw", () => {
    expect(poseValues([0, 0, 0, 0.1, 0.2, 0.3])).toEqual([0.1, 0.2, 0.3]);
    expect(poseValues({ roll: 1, pitch: "2", yaw: "x" })).toEqual([1, 2, null]);
    expect(poseValues(null)).toEqual([null, null, null]);
  });
  test("null entries stay unknown rather than reading as zero", () => {
    expect(poseValues([null, 0.5, undefined])).toEqual([null, 0.5, null]);
  });
  test("antennas: [right, left] array or left/right", () => {
    expect(antennaValues([0.1, -0.2])).toEqual([-0.2, 0.1]);
    expect(antennaValues({ left: 1, right: 2 })).toEqual([1, 2]);
    expect(antennaValues(undefined)).toEqual([null, null]);
  });
  test("doa: radians within ±2π, degrees otherwise", () => {
    expect(doaDegrees({ angle: Math.PI })).toBeCloseTo(180);
    expect(doaDegrees({ angle: 90 })).toBe(90);
    expect(doaDegrees({})).toBeNull();
    expect(doaDegrees(null)).toBeNull();
  });
});

describe("poseFrame", () => {
  test("readouts", () => {
    const f = poseFrame({
      head_pose: { roll: 0, pitch: Math.PI / 18, yaw: -Math.PI / 18 },
      body_yaw: 0.5,
      antennas_position: [0.25, -0.25],
      doa: { angle: Math.PI / 2, speech_detected: true },
    });
    expect(f.rpy).toBe("0/10/-10");
    expect(f.yaw).toBe("0.50");
    expect(f.ant).toBe("-0.25/0.25");
    expect(f.doa).toBe("90°");
    expect(f.speaking).toBe(true);
  });
  test("empty state renders dashes", () => {
    const f = poseFrame({});
    expect(f.rpy).toBe("—/—/—");
    expect(f.yaw).toBe("—");
    expect(f.ant).toBe("—/—");
    expect(f.doa).toBe("—°");
    expect(f.speaking).toBe(false);
  });
  test("speech without a direction is not 'speaking'", () => {
    expect(poseFrame({ doa: { speech_detected: true } }).speaking).toBe(false);
  });
});
