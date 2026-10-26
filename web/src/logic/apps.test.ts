import { describe, expect, test } from "bun:test";
import type { AppStatus } from "../api.ts";
import {
  APP_UI_HTTPS_PORT,
  activeApp,
  appAction,
  appUiUrl,
  cardStatus,
  formatUptime,
  normalizeFavorites,
  toggleFavoriteList,
  uptimeSuffix,
  visibleAppNames,
} from "./apps.ts";

const app = (name: string, status: string, over: Partial<AppStatus> = {}): AppStatus => ({ name, status, ...over });
const idle = { anyActive: false, appBusy: false, daemonOnline: true, exprBusy: false };

describe("activeApp", () => {
  test("running, loading and stopping all own the robot", () => {
    for (const s of ["running", "loading", "stopping"]) {
      expect(activeApp([app("a", "stopped"), app("b", s)])?.name).toBe("b");
    }
  });
  test("error and stopped do not", () => {
    expect(activeApp([app("a", "error"), app("b", "stopped")])).toBeNull();
  });
});

describe("cardStatus", () => {
  test("maps status to card and dot classes", () => {
    expect(cardStatus(app("a", "running"))).toEqual({ kind: "running", statusClass: "running", dotClass: "live" });
    expect(cardStatus(app("a", "loading")).dotClass).toBe("loading");
    expect(cardStatus(app("a", "stopping")).dotClass).toBe("stopping");
    expect(cardStatus(app("a", "error")).dotClass).toBe("error");
    expect(cardStatus(app("a", "stopped")).kind).toBe("");
    expect(cardStatus(app("a", "weird")).kind).toBe("");
  });
});

describe("normalizeFavorites", () => {
  test("dedupes, caps at three, drops non-strings", () => {
    expect(normalizeFavorites(["a", "a", 3, "b", "c", "d"], [])).toEqual(["a", "b", "c"]);
  });
  test("drops uninstalled apps once the installed list is known", () => {
    expect(normalizeFavorites(["a", "gone", "b"], ["a", "b"])).toEqual(["a", "b"]);
  });
  test("non-array is empty", () => {
    expect(normalizeFavorites(null, ["a"])).toEqual([]);
  });
});

describe("visibleAppNames", () => {
  const installed = ["a", "b", "c", "d"];
  test("favorites in installed order", () => {
    expect(visibleAppNames(installed, [], ["c", "a"])).toEqual(["a", "c"]);
  });
  test("active non-favorite and every errored non-favorite stay visible", () => {
    const statuses = [app("b", "running"), app("c", "error"), app("d", "error")];
    expect(visibleAppNames(installed, statuses, ["a"])).toEqual(["a", "b", "c", "d"]);
  });
});

describe("appAction", () => {
  test("running app: Open only with a UI, Stop locked while a request is in flight", () => {
    expect(appAction(app("a", "running", { has_ui: true }), { ...idle, appBusy: true })).toEqual({
      kind: "running",
      showOpen: true,
      stopDisabled: true,
    });
    expect(appAction(app("a", "running"), idle)).toMatchObject({ showOpen: false, stopDisabled: false });
  });
  test("loading/stopping show a disabled pending button", () => {
    expect(appAction(app("a", "loading"), idle)).toEqual({ kind: "pending", label: "Loading" });
    expect(appAction(app("a", "stopping"), idle)).toEqual({ kind: "pending", label: "Stopping" });
  });
  test("start is blocked by another active app, a busy request, offline daemon, or an expression", () => {
    expect(appAction(app("a", "stopped"), idle)).toEqual({ kind: "start", disabled: false, title: "" });
    expect(appAction(app("a", "stopped"), { ...idle, anyActive: true })).toMatchObject({ disabled: true });
    expect(appAction(app("a", "stopped"), { ...idle, appBusy: true })).toMatchObject({ disabled: true });
    expect(appAction(app("a", "error"), { ...idle, daemonOnline: false })).toEqual({
      kind: "start",
      disabled: true,
      title: "Apps start when the daemon is online.",
    });
    expect(appAction(app("a", "stopped"), { ...idle, exprBusy: true })).toEqual({
      kind: "start",
      disabled: true,
      title: "Apps start when the robot finishes its expression.",
    });
  });
});

describe("favorites toggle", () => {
  test("adds and removes", () => {
    expect(toggleFavoriteList(["a"], "b")).toEqual({ ok: true, next: ["a", "b"] });
    expect(toggleFavoriteList(["a", "b"], "a")).toEqual({ ok: true, next: ["b"] });
  });
  test("refuses a fourth", () => {
    expect(toggleFavoriteList(["a", "b", "c"], "d")).toEqual({ ok: false, error: "Choose up to 3 favorite apps." });
  });
});

describe("uptime", () => {
  test("formats minutes and hours", () => {
    expect(formatUptime(7)).toBe("0m 07s");
    expect(formatUptime(187)).toBe("3m 07s");
    expect(formatUptime(3 * 3600 + 5 * 60 + 9)).toBe("3h 05m");
    expect(formatUptime(-1)).toBe("");
    expect(formatUptime(Number.NaN)).toBe("");
  });
  test("only running apps with a start time tick", () => {
    expect(uptimeSuffix(app("a", "running", { start_time: 100 }), 287_000)).toBe(" · 3m 07s");
    expect(uptimeSuffix(app("a", "running", { start_time: null }), 287_000)).toBe("");
    expect(uptimeSuffix(app("a", "loading", { start_time: 100 }), 287_000)).toBe("");
  });
});

describe("appUiUrl", () => {
  test("plain http pairs the dashboard host with the app port", () => {
    expect(appUiUrl("reachy.tailnet", 8042, "http:")).toBe("http://reachy.tailnet:8042/");
    expect(appUiUrl("reachy.tailnet", 7860, "http:")).toBe("http://reachy.tailnet:7860/");
  });
  test("https goes through the app proxy port, whatever the app's port", () => {
    expect(appUiUrl("reachy.tailnet", 8042, "https:")).toBe(`https://reachy.tailnet:${APP_UI_HTTPS_PORT}/`);
    expect(appUiUrl("reachy.tailnet", 7860, "https:")).toBe("https://reachy.tailnet:8043/");
  });
  test("defaults to http without a page location", () => {
    expect(appUiUrl("reachy.tailnet", 8042)).toBe("http://reachy.tailnet:8042/");
  });
});
