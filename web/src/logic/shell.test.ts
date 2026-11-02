import { describe, expect, test } from "bun:test";
import { INITIAL_POWER, type PowerView } from "./power.ts";
import { parseTab, robotStatus, stageNote } from "./shell.ts";

const view = (v: Partial<PowerView>): PowerView => ({ ...INITIAL_POWER, ...v });

describe("shell", () => {
  test("unknown saved tabs open the robot tab", () => {
    expect(parseTab("apps")).toBe("apps");
    expect(parseTab("nope")).toBe("robot");
    expect(parseTab(null)).toBe("robot");
  });
  test("robot status names the state, and the app that owns the robot", () => {
    expect(robotStatus(view({ powerClass: "running" }), true, null)).toEqual({ text: "On", dot: "ok" });
    expect(robotStatus(view({ powerClass: "running" }), true, "Radio")).toEqual({ text: "Radio", dot: "live" });
    expect(robotStatus(view({ powerClass: "disabled" }), false, null).dot).toBe("error");
    expect(robotStatus(view({ powerClass: "stopping", label: "Powering off…" }), true, null).text).toBe("Powering off…");
    expect(robotStatus(view({}), true, null).text).toBe("Off");
    expect(robotStatus(view({}), false, null).text).toBe("Connecting…");
    // Off: the status poll answered, though the state stream is down.
    expect(robotStatus(view({ known: true }), false, null).text).toBe("Off");
  });
  test("the stage says why there is no robot", () => {
    expect(stageNote(view({ known: true, powerClass: "running" }), true)).toBeNull();
    expect(stageNote(view({}), false)?.title).toBe("Connecting…");
    expect(stageNote(view({ known: true }), false)?.action).toBe("Power on");
    expect(stageNote(view({ known: true, powerClass: "disabled" }), false)?.title).toBe("Robot service down");
    expect(stageNote(view({ known: true, powerClass: "starting" }), false)?.title).toBe("Waking up…");
    // Powering off can't be woken from the button; asleep can.
    expect(stageNote(view({ known: true, powerClass: "stopping", label: "Powering off…" }), false)?.action).toBeUndefined();
    expect(stageNote(view({ known: true, powerClass: "stopping" }), false)?.action).toBe("Wake");
  });
});
