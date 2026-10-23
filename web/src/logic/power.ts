import type { DaemonStatus } from "../api.ts";

export type PowerClass = "" | "running" | "starting" | "stopping" | "disabled";

export interface PowerView {
  powerClass: PowerClass;
  title: string;
  disabled: boolean;
  /** Text under the power button; empty hides it. */
  label: string;
}

export const INITIAL_POWER: PowerView = {
  powerClass: "",
  title: "Start daemon",
  disabled: false,
  label: "",
};

/** A daemon that stays unreachable for a minute is offline, not powering off. */
export const STOPPING_TIMEOUT_MS = 60_000;

const OFF_STATES = ["stopped", "not_initialized", "error"];

/**
 * Power-button presentation for one /api/daemon/status poll.
 *
 * `stopping` is the dashboard's own "a power-off is in flight" flag (set by the
 * power button and the kinematics switch). Returns the view and whether that
 * flag should stay set after this poll.
 */
export function derivePower(
  status: DaemonStatus,
  stopping: boolean,
  stoppingSince: number,
  now: number,
): { view: PowerView; stopping: boolean } {
  let stillStopping = stopping && now - stoppingSince <= STOPPING_TIMEOUT_MS;

  if (stillStopping && !OFF_STATES.includes(status.status)) {
    // Power-off in progress. The daemon plays the sleep move (motors on), then
    // releases torque and tears the backend down; that teardown blocks the
    // daemon for a few seconds, so an unreachable daemon still means
    // "powering off", not "offline".
    const sleeping = status.running || (status.status === "stopping" && status.motors === "enabled");
    const label = sleeping ? "Shutting down…" : "Powering off…";
    return { view: { powerClass: "stopping", label, title: label, disabled: true }, stopping: true };
  }
  stillStopping = false;

  if (!status.reachable) {
    return {
      view: { powerClass: "disabled", title: "Daemon service offline", disabled: true, label: "" },
      stopping: stillStopping,
    };
  }
  if (status.running) {
    return {
      view: { powerClass: "running", title: "Switch robot off", disabled: false, label: "" },
      stopping: stillStopping,
    };
  }
  if (status.status === "asleep") {
    // Backend up, motors released (e.g. the daemon slept after an app exited).
    return {
      view: { powerClass: "stopping", title: "Robot asleep — wake up", disabled: false, label: "" },
      stopping: stillStopping,
    };
  }
  if (status.status === "starting") {
    return {
      view: { powerClass: "starting", title: "Robot starting…", disabled: true, label: "Starting…" },
      stopping: stillStopping,
    };
  }
  if (status.status === "stopping") {
    // Stopped from elsewhere (another tab, the kinematics switch).
    return {
      view: { powerClass: "stopping", title: "Powering off…", disabled: true, label: "Powering off…" },
      stopping: stillStopping,
    };
  }
  return {
    view: { powerClass: "", title: "Switch robot on", disabled: false, label: "" },
    stopping: stillStopping,
  };
}
