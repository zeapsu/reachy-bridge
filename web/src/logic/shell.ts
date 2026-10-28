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
      return { text: "Daemon offline", dot: "error" };
    case "running":
      return activeApp ? { text: `On · ${activeApp}`, dot: "live" } : { text: "On", dot: "ok" };
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
  title: string;
  detail: string;
  dot: RobotStatus["dot"];
}

/** What the robot stage says while there is no live robot to show; null when there is. */
export function stageNote(power: PowerView, daemonOnline: boolean): StageNote | null {
  if (daemonOnline) return null;
  if (!power.known) return { title: "Connecting…", detail: "Looking for the robot.", dot: "loading" };
  switch (power.powerClass) {
    case "disabled":
      return { title: "Daemon offline", detail: "The robot service isn't answering.", dot: "error" };
    case "starting":
      return { title: "Waking up…", detail: "Reachy will appear in a moment.", dot: "loading" };
    case "running":
      return { title: "Connecting…", detail: "Waiting for the robot's live state.", dot: "loading" };
    case "stopping":
      // A power-off in flight disables the button; only asleep can be woken.
      return power.label
        ? { title: power.label, detail: "Reachy is going to sleep.", dot: "loading" }
        : { title: "Asleep", detail: "Press the power button to wake Reachy.", dot: "loading" };
    default:
      return { title: "Reachy is off", detail: "Press the power button to switch it on.", dot: "" };
  }
}
