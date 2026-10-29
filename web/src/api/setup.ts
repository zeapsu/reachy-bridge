// The setup page (#135), served on its own port by `reachy-bridge setup`.

export const setupPaths = {
  setupStatus: "/api/setup/status",
  setupLogin: "/api/setup/login",
  setupCheck: "/api/setup/check",
  setupAdmin: "/api/setup/admin",
  setupRobotTest: "/api/setup/robot-test",
  setupFinish: "/api/setup/finish",
} as const;

export interface SetupStatus {
  setup: true;
  authenticated: boolean;
  /** Five wrong codes: only a new code from the terminal unlocks it. */
  locked: boolean;
  /** The code was used (by this browser or another). */
  used: boolean;
  /** Seconds until the page closes. */
  expires_in: number;
}

/** Where the admin request stands; the robot's terminal answers it. */
export interface AdminRequest {
  status: "none" | "waiting" | "approved" | "refused";
  detail?: string;
}

/** The robot test; the robot's terminal says yes before it runs. */
export interface RobotTestState {
  status: "none" | "asked" | "running" | "passed" | "failed";
  detail?: string;
}

export interface CheckResult {
  name: string;
  status: "pass" | "warn" | "fail" | "skip";
  detail: string;
}
