import { Euler, Matrix4, Quaternion, Vector3 } from "three";
import rigData from "../assets/reachy_mini.rig.json";

/**
 * Pose the 3D model from the daemon's state (#149). The model's meshes are
 * baked in world coordinates at the neutral pose (tools/model/export.py), so
 * each part only needs one matrix that moves it from neutral to now:
 *
 * - body, horns and rods turn with `body_yaw` about the vertical axis;
 * - each horn also turns about its hinge by its `head_joints` angle minus the
 *   neutral one;
 * - the head and antennas follow `head_pose` (a world pose about the head
 *   pivot, R = Rz(yaw)·Ry(pitch)·Rx(roll), as the SDK builds it);
 * - each rod pivots at its horn tip so its far end meets the head.
 *
 * Checked against MuJoCo at a real daemon pose (rig.test.ts).
 */

export interface Hinge {
  anchor: number[];
  axis: number[];
  neutral: number;
}

export interface Rig {
  head_pivot: number[];
  horns: Hinge[];
  rods: { start: number[]; end: number[] }[];
  antennas: { right: Hinge; left: Hinge };
}

export interface RigState {
  head_pose?: { x?: number; y?: number; z?: number; roll?: number; pitch?: number; yaw?: number } | null;
  head_joints?: number[] | null;
  body_yaw?: number | null;
  /** [right, left], radians. */
  antennas_position?: number[] | null;
}

export const RIG = rigData as Rig;

export type PartMatrices = Record<string, Matrix4>;

const v = (a: number[]) => new Vector3(a[0], a[1], a[2]);

/** A rotation by `angle` about the line through `point` along `axis`. */
function about(point: Vector3, axis: Vector3, angle: number): Matrix4 {
  const rotate = new Matrix4().makeRotationAxis(axis.clone().normalize(), angle);
  return new Matrix4()
    .makeTranslation(point.x, point.y, point.z)
    .multiply(rotate)
    .multiply(new Matrix4().makeTranslation(-point.x, -point.y, -point.z));
}

export function headMatrix(state: RigState, rig: Rig = RIG): Matrix4 {
  const p = state.head_pose ?? {};
  const pivot = v(rig.head_pivot);
  const rotation = new Matrix4().makeRotationFromEuler(new Euler(p.roll ?? 0, p.pitch ?? 0, p.yaw ?? 0, "ZYX"));
  return new Matrix4()
    .makeTranslation(pivot.x + (p.x ?? 0), pivot.y + (p.y ?? 0), pivot.z + (p.z ?? 0))
    .multiply(rotation)
    .multiply(new Matrix4().makeTranslation(-pivot.x, -pivot.y, -pivot.z));
}

/** One matrix per model part (the GLB's node names), neutral → `state`. */
export function partMatrices(state: RigState, rig: Rig = RIG): PartMatrices {
  const up = new Vector3(0, 0, 1);
  const yaw = new Matrix4().makeRotationAxis(up, state.body_yaw ?? 0);
  const head = headMatrix(state, rig);
  const out: PartMatrices = { base: new Matrix4(), body: yaw.clone(), head };

  const ant = state.antennas_position;
  for (const [name, hinge, angle] of [
    ["antenna_right", rig.antennas.right, ant?.[0]],
    ["antenna_left", rig.antennas.left, ant?.[1]],
  ] as const) {
    const spin = about(v(hinge.anchor), v(hinge.axis), (angle ?? hinge.neutral) - hinge.neutral);
    out[name] = head.clone().multiply(spin);
  }

  rig.horns.forEach((hinge, i) => {
    const angle = state.head_joints?.[i + 1];
    const horn = yaw.clone().multiply(about(v(hinge.anchor), v(hinge.axis), (angle ?? hinge.neutral) - hinge.neutral));
    out[`horn${i + 1}`] = horn;
    const rod = rig.rods[i];
    const start0 = v(rod.start);
    const start = start0.clone().applyMatrix4(horn);
    const end = v(rod.end).applyMatrix4(head);
    // Carry the rod with its horn, then swing it about the tip onto the head.
    const carried = v(rod.end).applyMatrix4(horn).sub(start).normalize();
    const swing = new Quaternion().setFromUnitVectors(carried, end.sub(start).normalize());
    out[`rod${i + 1}`] = new Matrix4()
      .makeTranslation(start.x, start.y, start.z)
      .multiply(new Matrix4().makeRotationFromQuaternion(swing))
      .multiply(new Matrix4().makeTranslation(-start.x, -start.y, -start.z))
      .multiply(horn);
  });
  return out;
}
