import type { PowerView } from "./power.ts";

export const TABS = ["robot", "apps", "system"] as const;
export type Tab = (typeof TABS)[number];
export const TAB_KEY = "reachy-tab";

export function parseTab(value: string | null): Tab {
  return TABS.includes(value as Tab) ? (value as Tab) : "robot";
}

export interface RobotStatus {
  text: string;
  dot: "ok" | "live" | "loading" | "error" | "";
}

/** The top bar's one-line robot status. */
export function robotStatus(power: PowerView, daemonOnline: boolean, activeApp: string | null): RobotStatus {
  if (power.label) return { text: power.label, dot: "loading" };
  switch (power.powerClass) {
    case "disabled":
      return { text: "Robot service down", dot: "error" };
    case "running":
      return activeApp ? { text: activeApp, dot: "live" } : { text: "On", dot: "ok" };
    case "stopping":
      return { text: "Asleep", dot: "loading" };
    case "starting":
      return { text: "Starting…", dot: "loading" };
    default:
      // The state stream is down whenever the robot is off, so only an
      // unanswered status poll means we're still connecting.
      return daemonOnline || power.known ? { text: "Off", dot: "" } : { text: "Connecting…", dot: "" };
  }
}

export interface StageNote {
  /** Empty when the button says it all. */
  title: string;
  dot: RobotStatus["dot"];
  /** The power button's next action, shown on the stage. */
  action?: "Power on" | "Wake";
}

/** What the robot stage says while there is no live robot to show; null when there is. */
export function stageNote(power: PowerView, daemonOnline: boolean): StageNote | null {
  if (daemonOnline) return null;
  if (!power.known) return { title: "Connecting…", dot: "loading" };
  switch (power.powerClass) {
    case "disabled":
      return { title: "Robot service down", dot: "error" };
    case "starting":
      return { title: "Waking up…", dot: "loading" };
    case "running":
      return { title: "Connecting…", dot: "loading" };
    case "stopping":
      // A power-off in flight disables the button; only asleep can be woken.
      return power.label
        ? { title: power.label, dot: "loading" }
        : { title: "", dot: "", action: "Wake" };
    default:
      return { title: "", dot: "", action: "Power on" };
  }
}
