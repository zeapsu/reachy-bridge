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
      return daemonOnline ? { text: "Off", dot: "" } : { text: "Connecting…", dot: "" };
  }
}
