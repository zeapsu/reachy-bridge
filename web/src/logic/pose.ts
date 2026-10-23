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

const polar = (cx: number, cy: number, radius: number, degrees: number): [number, number] => {
  const radians = ((degrees - 90) * Math.PI) / 180;
  return [cx + radius * Math.cos(radians), cy + radius * Math.sin(radians)];
};

/** The DOA arc around the rig (c160,108 r92) and the tick at its centre. */
export function doaArc(degrees: number): { d: string; tickX: string; tickY: string } {
  const cx = 160;
  const cy = 108;
  const radius = 92;
  const span = 24;
  const [x1, y1] = polar(cx, cy, radius, degrees - span);
  const [x2, y2] = polar(cx, cy, radius, degrees + span);
  const [tx, ty] = polar(cx, cy, radius, degrees);
  return {
    d: `M ${x1.toFixed(1)} ${y1.toFixed(1)} A ${radius} ${radius} 0 0 1 ${x2.toFixed(1)} ${y2.toFixed(1)}`,
    tickX: tx.toFixed(1),
    tickY: ty.toFixed(1),
  };
}

/** Everything the schematic and HUD render for one state-update. */
export interface PoseFrame {
  rpy: string;
  yaw: string;
  ant: string;
  doa: string;
  head: string;
  body: string;
  antL: string;
  antR: string;
  speaking: boolean;
  doaDeg: number | null;
}

export function poseFrame(payload: RobotState): PoseFrame {
  const [rollDeg, pitchDeg, yawDeg] = poseValues(payload.head_pose).map(radiansToDegrees);
  const bodyYaw = finite(payload.body_yaw);
  const [antLeft, antRight] = antennaValues(payload.antennas_position);
  const doa = payload.doa;
  const doaDeg = doaDegrees(doa);
  const speech = Boolean(doa && typeof doa === "object" && doa.speech_detected === true);
  const roll = rollDeg ?? 0;
  const pitch = pitchDeg ?? 0;
  const yaw = yawDeg ?? 0;
  return {
    rpy: [rollDeg, pitchDeg, yawDeg].map((v) => (v == null ? "—" : v.toFixed(0))).join("/"),
    yaw: dash(bodyYaw, 2),
    ant: `${dash(antLeft, 2)}/${dash(antRight, 2)}`,
    doa: doaDeg == null ? "—°" : `${doaDeg.toFixed(0)}°`,
    head: `rotate(${roll.toFixed(2)} 160 104) translate(${(yaw * 0.3).toFixed(2)} ${(-pitch * 0.3).toFixed(2)})`,
    body: `rotate(${((bodyYaw ?? 0) * 12).toFixed(2)} 160 152)`,
    antL: `rotate(${((antLeft ?? 0) * 18).toFixed(2)} 132 70)`,
    antR: `rotate(${((antRight ?? 0) * 18).toFixed(2)} 188 70)`,
    speaking: speech && doaDeg != null,
    doaDeg,
  };
}
