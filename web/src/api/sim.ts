// Sim mode (#149): the controller drives a simulated robot instead of the real one.

export type Target = "robot" | "sim";

/** GET /api/sim, and the answer to POST /api/sim/enter and /api/sim/exit */
export interface SimStatus {
  active: boolean;
  target: Target;
}

export const simPaths = {
  sim: "/api/sim",
  simEnter: "/api/sim/enter",
  simExit: "/api/sim/exit",
  /** SSE of the sim's sounds (#219), `sim-sounds` events. */
  simSoundsStream: "/api/sim/sounds-stream",
  simSound: (token: string) => `/api/sim/sounds/${encodeURIComponent(token)}`,
} as const;
