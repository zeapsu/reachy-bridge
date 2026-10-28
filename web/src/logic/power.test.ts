import { describe, expect, test } from "bun:test";
import type { DaemonStatus } from "../api.ts";
import { STOPPING_TIMEOUT_MS, derivePower } from "./power.ts";

const status = (over: Partial<DaemonStatus>): DaemonStatus => ({
  reachable: true,
  running: false,
  manageable: true,
  status: "stopped",
  ...over,
});

describe("derivePower", () => {
  test("unreachable daemon disables the button", () => {
    const { view } = derivePower(status({ reachable: false, status: "offline" }), false, 0, 0);
    expect(view).toEqual({ known: true, powerClass: "disabled", title: "Daemon service offline", disabled: true, label: "" });
  });

  test("running robot offers switch-off", () => {
    expect(derivePower(status({ running: true, status: "running" }), false, 0, 0).view).toMatchObject({
      powerClass: "running",
      title: "Switch robot off",
      disabled: false,
    });
  });

  test("asleep is wakeable, not off (running backend, motors released)", () => {
    expect(derivePower(status({ status: "asleep" }), false, 0, 0).view).toMatchObject({
      powerClass: "stopping",
      title: "Robot asleep — wake up",
      disabled: false,
      label: "",
    });
  });

  test("starting and external stopping lock the button with a label", () => {
    expect(derivePower(status({ status: "starting" }), false, 0, 0).view).toMatchObject({
      powerClass: "starting",
      label: "Starting…",
      disabled: true,
    });
    expect(derivePower(status({ status: "stopping" }), false, 0, 0).view).toMatchObject({
      powerClass: "stopping",
      label: "Powering off…",
      disabled: true,
    });
  });

  test("stopped robot offers switch-on", () => {
    expect(derivePower(status({ status: "stopped" }), false, 0, 0).view).toMatchObject({
      powerClass: "",
      title: "Switch robot on",
      disabled: false,
    });
  });

  test("our power-off shows 'Shutting down…' while the sleep move runs", () => {
    const r = derivePower(status({ running: true, status: "running" }), true, 1000, 2000);
    expect(r.stopping).toBe(true);
    expect(r.view).toMatchObject({ powerClass: "stopping", label: "Shutting down…", disabled: true });
    const sleepMove = derivePower(status({ status: "stopping", motors: "enabled" }), true, 1000, 2000);
    expect(sleepMove.view.label).toBe("Shutting down…");
  });

  test("an unreachable daemon during our power-off still reads as powering off", () => {
    const r = derivePower(status({ reachable: false, status: "offline" }), true, 1000, 2000);
    expect(r.stopping).toBe(true);
    expect(r.view).toMatchObject({ powerClass: "stopping", label: "Powering off…", disabled: true });
  });

  test("stopping flag clears once the daemon reports off", () => {
    for (const s of ["stopped", "not_initialized", "error"]) {
      const r = derivePower(status({ status: s }), true, 1000, 2000);
      expect(r.stopping).toBe(false);
      expect(r.view.powerClass).toBe("");
    }
  });

  test("stopping flag expires after a minute of no progress", () => {
    const r = derivePower(status({ reachable: false, status: "offline" }), true, 0, STOPPING_TIMEOUT_MS + 1);
    expect(r.stopping).toBe(false);
    expect(r.view.powerClass).toBe("disabled");
  });
});
