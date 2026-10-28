// Sim mode (#149): keeping the page's idea of the target in step with the controller's.

import type { Target } from "../api/sim.ts";

/**
 * What to do with the target one status poll reports. The page adopts the
 * first one it sees; after that a different target means the controller was
 * switched elsewhere (another tab, another device), and everything the page
 * shows (power, moves, apps) is about the other robot: reload. A switch this
 * page is making itself is waited out, until a poll reports its result
 * (`pending`).
 */
export function syncTarget(
  believed: Target | null,
  reported: Target | undefined,
  switching: boolean,
  pending: Target | null = null,
): "adopt" | "keep" | "reload" {
  if (!reported || switching) return "keep";
  if (pending !== null && reported !== pending) return "keep";
  if (believed === null) return "adopt";
  return believed === reported ? "keep" : "reload";
}

export interface SimView {
  /** The button's label. */
  action: string;
  disabled: boolean;
  /** Why it is disabled, or what it does. */
  hint: string;
}

/** The System tab's sim card. Entering needs the robot off. */
export function simView(target: Target | null, switching: boolean, robotOff: boolean): SimView {
  if (switching) {
    return { action: target === "sim" ? "Stopping…" : "Starting…", disabled: true, hint: "The simulated robot takes up to a minute to start." };
  }
  if (target === "sim") {
    return { action: "Back to the robot", disabled: false, hint: "The dashboard is driving a simulated Reachy, apps included. The real robot stays off." };
  }
  if (!robotOff) {
    return { action: "Try the simulation", disabled: true, hint: "Switch the robot off to try the simulation." };
  }
  return { action: "Try the simulation", disabled: target === null, hint: "Drive a simulated Reachy instead of the real one. Its apps are its own; sound and camera are off." };
}
