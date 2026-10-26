import type { RobotState } from "../api.ts";

const TAU = Math.PI * 2;

export const finite = (value: unknown): number | null => {
  if (value === null || value === undefined) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};
const radiansToDegrees = (value: number | null) => (value == null ? null : (value * 180) / Math.PI);
const dash = (value: number | null, digits = 2) => (value == null ? "—" : value.toFixed(digits));

export function poseValues(pose: unknown): [number | null, number | null, number | null] {
  if (Array.isArray(pose)) {
    const last = pose.slice(-3).map(finite);
    return [last[0] ?? null, last[1] ?? null, last[2] ?? null];
  }
  if (pose && typeof pose === "object") {
    const p = pose as Record<string, unknown>;
    return [finite(p.roll), finite(p.pitch), finite(p.yaw)];
  }
  return [null, null, null];
}

export function antennaValues(value: unknown): [number | null, number | null] {
  if (Array.isArray(value)) return [finite(value[0]), finite(value[1])];
  if (value && typeof value === "object") {
    const v = value as Record<string, unknown>;
    return [finite(v.left), finite(v.right)];
  }
  return [null, null];
}

/** DOA in degrees; values within ±2π are radians, larger ones already degrees. */
export function doaDegrees(doa: unknown): number | null {
  if (!doa || typeof doa !== "object") return null;
  const angle = finite((doa as Record<string, unknown>).angle);
  if (angle == null) return null;
  return Math.abs(angle) <= TAU ? radiansToDegrees(angle) : angle;
}

/** Everything the HUD renders for one state-update (the 3D view poses itself). */
export interface PoseFrame {
  rpy: string;
  yaw: string;
  ant: string;
  doa: string;
  speaking: boolean;
}

export function poseFrame(payload: RobotState): PoseFrame {
  const [rollDeg, pitchDeg, yawDeg] = poseValues(payload.head_pose).map(radiansToDegrees);
  const bodyYaw = finite(payload.body_yaw);
  const [antLeft, antRight] = antennaValues(payload.antennas_position);
  const doa = payload.doa;
  const doaDeg = doaDegrees(doa);
  const speech = Boolean(doa && typeof doa === "object" && doa.speech_detected === true);
  return {
    rpy: [rollDeg, pitchDeg, yawDeg].map((v) => (v == null ? "—" : v.toFixed(0))).join("/"),
    yaw: dash(bodyYaw, 2),
    ant: `${dash(antLeft, 2)}/${dash(antRight, 2)}`,
    doa: doaDeg == null ? "—°" : `${doaDeg.toFixed(0)}°`,
    speaking: speech && doaDeg != null,
  };
}
