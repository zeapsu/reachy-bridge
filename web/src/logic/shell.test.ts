import { describe, expect, test } from "bun:test";
import { INITIAL_POWER, type PowerView } from "./power.ts";
import { parseTab, robotStatus } from "./shell.ts";

const view = (v: Partial<PowerView>): PowerView => ({ ...INITIAL_POWER, ...v });

describe("shell", () => {
  test("unknown saved tabs open the robot tab", () => {
    expect(parseTab("apps")).toBe("apps");
    expect(parseTab("nope")).toBe("robot");
    expect(parseTab(null)).toBe("robot");
  });
  test("robot status names the state, and the app that owns the robot", () => {
    expect(robotStatus(view({ powerClass: "running" }), true, null)).toEqual({ text: "On", dot: "ok" });
    expect(robotStatus(view({ powerClass: "running" }), true, "Radio")).toEqual({ text: "On · Radio", dot: "live" });
    expect(robotStatus(view({ powerClass: "disabled" }), false, null).dot).toBe("error");
    expect(robotStatus(view({ powerClass: "stopping", label: "Powering off…" }), true, null).text).toBe("Powering off…");
    expect(robotStatus(view({}), true, null).text).toBe("Off");
    expect(robotStatus(view({}), false, null).text).toBe("Connecting…");
  });
});
