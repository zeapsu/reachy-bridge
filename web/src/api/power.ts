// Robot power, motors and the kinematics engine.

/** GET /api/daemon/status */
export interface DaemonStatus {
  reachable: boolean;
  running: boolean;
  manageable: boolean;
  /** offline | running | asleep | starting | stopping | stopped | not_initialized | error | ... */
  status: string;
  motors?: string | null;
  error?: unknown;
  /** Which daemon the controller drives (#149). */
  target?: "robot" | "sim";
}

/** GET/PUT /api/daemon/kinematics */
export interface Kinematics {
  engine: string;
  active: string | null;
  available: string[];
  restarted?: boolean;
}

/** GET /api/motors/status */
export interface MotorStatus {
  mode: string;
}

export const powerPaths = {
  daemonStatus: "/api/daemon/status",
  daemonStart: "/api/daemon/start",
  daemonStop: "/api/daemon/stop",
  kinematics: "/api/daemon/kinematics",
  motorStatus: "/api/motors/status",
  motorSetMode: (mode: string) => `/api/motors/set_mode/${encodeURIComponent(mode)}`,
} as const;
