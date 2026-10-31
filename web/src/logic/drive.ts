/**
 * Manual drive (#152): pad positions to the targets `/api/drive/ws` takes.
 * The controller clamps every value to the robot's limits; the speed limit
 * here keeps a flick of the thumb from becoming a jump.
 */

const DEG = Math.PI / 180;

/** The controller's limits (routes/drive.rs), replaced by its `ready` message. */
export interface DriveLimits {
  pitch: number;
  /** Head yaw relative to the body. */
  headYaw: number;
  bodyYaw: number;
  z: [number, number];
  antenna: number;
}

export const DEFAULT_LIMITS: DriveLimits = {
  pitch: 40 * DEG,
  headYaw: 65 * DEG,
  bodyYaw: 160 * DEG,
  z: [-0.03, 0.02],
  antenna: 90 * DEG,
};

/** Read the `ready` message's limits, keeping the defaults for anything missing. */
export function parseLimits(message: unknown): DriveLimits {
  const limits = (message as { limits?: Record<string, unknown> } | null)?.limits;
  const head = (limits?.head ?? {}) as Record<string, unknown>;
  const high = (range: unknown, fallback: number) =>
    Array.isArray(range) && Number.isFinite(range[1]) ? Math.abs(range[1] as number) : fallback;
  const z = head.z;
  return {
    pitch: high(head.pitch, DEFAULT_LIMITS.pitch),
    headYaw: high(head.yaw_from_body, DEFAULT_LIMITS.headYaw),
    bodyYaw: high(limits?.body_yaw, DEFAULT_LIMITS.bodyYaw),
    z:
      Array.isArray(z) && Number.isFinite(z[0]) && Number.isFinite(z[1])
        ? [z[0] as number, z[1] as number]
        : DEFAULT_LIMITS.z,
    antenna: high(limits?.antennas, DEFAULT_LIMITS.antenna),
  };
}

/** The antennas' park angles, daemon order [right, left] (expressions.rs). */
export const ANTENNA_PARK: [number, number] = [-0.1745, 0.1745];

/** Thumb positions, each axis -1..1 with y pointing down the screen. */
export interface Sticks {
  head: { x: number; y: number };
  body: { x: number; y: number };
  /** -1..1 around the park angle, [left, right] as the sliders sit. */
  antennas: [number, number];
}

export const CENTRED: Sticks = { head: { x: 0, y: 0 }, body: { x: 0, y: 0 }, antennas: [0, 0] };

export function centred(sticks: Sticks): boolean {
  const { head, body, antennas } = sticks;
  return !head.x && !head.y && !body.x && !body.y && !antennas[0] && !antennas[1];
}

/** Hold mode (per browser): a control let go stays where it was, and so does the robot. */
export const HOLD_KEY = "reachy-drive-hold";

/** A knob position kept inside the pad's circle. */
export function inPad(x: number, y: number): { x: number; y: number } {
  const length = Math.hypot(x, y);
  return length > 1 ? { x: x / length, y: y / length } : { x, y };
}

/** Robot targets in radians and metres; head yaw is relative to the body. */
export interface Pose {
  pitch: number;
  headYaw: number;
  bodyYaw: number;
  z: number;
  /** Daemon order [right, left]. */
  antennas: [number, number];
}

export const HOME: Pose = { pitch: 0, headYaw: 0, bodyYaw: 0, z: 0, antennas: [...ANTENNA_PARK] };

const unit = (v: number) => (Number.isFinite(v) ? Math.max(-1, Math.min(1, v)) : 0);

/**
 * Head pad: right looks right, down looks down. Body pad: right turns the
 * body right, up raises the head and down lowers it. A slider swings its
 * antenna either side of park, mirrored so + moves both the same way.
 */
export function stickPose(sticks: Sticks, limits: DriveLimits = DEFAULT_LIMITS): Pose {
  const by = unit(sticks.body.y);
  const swing = limits.antenna - Math.abs(ANTENNA_PARK[1]);
  const [left, right] = sticks.antennas.map(unit) as [number, number];
  // `+ 0` turns -0 into 0, so a centred stick is exactly home.
  return {
    pitch: unit(sticks.head.y) * limits.pitch + 0,
    headYaw: -unit(sticks.head.x) * limits.headYaw + 0,
    bodyYaw: -unit(sticks.body.x) * limits.bodyYaw + 0,
    z: (by < 0 ? -by * limits.z[1] : by * limits.z[0]) + 0,
    antennas: [ANTENNA_PARK[0] - right * swing, ANTENNA_PARK[1] + left * swing],
  };
}

/** Top speeds, per second. */
export const MAX_SPEED = { angle: 90 * DEG, bodyYaw: 90 * DEG, z: 0.04, antenna: 180 * DEG };

const toward = (from: number, to: number, step: number) =>
  Math.abs(to - from) <= step ? to : from + Math.sign(to - from) * step;

/** One tick of `dt` seconds from `pose` toward `goal`, no axis faster than MAX_SPEED. */
export function stepToward(pose: Pose, goal: Pose, dt: number): Pose {
  const t = Math.max(0, Math.min(dt, 0.1));
  return {
    pitch: toward(pose.pitch, goal.pitch, MAX_SPEED.angle * t),
    headYaw: toward(pose.headYaw, goal.headYaw, MAX_SPEED.angle * t),
    bodyYaw: toward(pose.bodyYaw, goal.bodyYaw, MAX_SPEED.bodyYaw * t),
    z: toward(pose.z, goal.z, MAX_SPEED.z * t),
    antennas: [
      toward(pose.antennas[0], goal.antennas[0], MAX_SPEED.antenna * t),
      toward(pose.antennas[1], goal.antennas[1], MAX_SPEED.antenna * t),
    ],
  };
}

export function samePose(a: Pose, b: Pose): boolean {
  return (
    a.pitch === b.pitch &&
    a.headYaw === b.headYaw &&
    a.bodyYaw === b.bodyYaw &&
    a.z === b.z &&
    a.antennas[0] === b.antennas[0] &&
    a.antennas[1] === b.antennas[1]
  );
}

/** The socket message for a pose. The controller takes head yaw as absolute. */
export function driveMessage(pose: Pose): string {
  return JSON.stringify({
    head: { z: pose.z, pitch: pose.pitch, yaw: pose.bodyYaw + pose.headYaw },
    body_yaw: pose.bodyYaw,
    antennas: pose.antennas,
  });
}

export interface DriveGate {
  daemonOnline: boolean;
  motorMode: string | null;
  appActive: boolean;
  exprBusy: boolean;
  controlBusy: boolean;
}

/** Why the pads are off right now, or null when the robot can be driven. */
export function driveBlocked(gate: DriveGate): string | null {
  if (!gate.daemonOnline) return "Drive works when the robot is on.";
  if (gate.appActive) return "An app is driving the robot.";
  if (gate.motorMode !== "enabled") return "Set Motors to Enabled to drive.";
  if (gate.exprBusy) return "Wait for the move to finish.";
  if (gate.controlBusy) return "Wait for the robot to settle.";
  return null;
}
