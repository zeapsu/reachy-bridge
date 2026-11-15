import { describe, expect, test } from "bun:test";
import { allowedApps, appsText, profileInitial, rights } from "./profile.ts";

const kid = { name: "Mia", admin: false, shared: false, kind: "kid" as const, apps: ["dance"], can: { power: false, drive: true, voice: false, camera: false } };

describe("rights", () => {
  test("no profile (no admin mode) allows everything", () => {
    const r = rights(null);
    expect([r.family, r.system, r.controls, r.can.power, r.apps, r.picking]).toEqual([false, true, true, true, null, false]);
  });
  test("the admin has everything", () => {
    const r = rights({ ...kid, admin: true });
    expect([r.family, r.system, r.controls, r.can.camera, r.apps]).toEqual([true, true, true, true, null]);
  });
  test("a kid loses System and Controls and keeps its switches", () => {
    const r = rights(kid);
    expect([r.system, r.controls, r.can.power, r.can.drive, r.apps]).toEqual([false, false, false, true, ["dance"]]);
  });
  test("a grown-up keeps Controls but not System", () => {
    const r = rights({ ...kid, kind: "grown" });
    expect([r.system, r.controls]).toEqual([false, true]);
  });
  test("a shared device without a pick is picking", () => {
    expect(rights({ ...kid, name: null, shared: true }).picking).toBe(true);
    expect(rights({ ...kid, shared: true }).picking).toBe(false);
  });
});

test("allowedApps keeps order and filters", () => {
  expect(allowedApps(["a", "b", "c"], null)).toEqual(["a", "b", "c"]);
  expect(allowedApps(["a", "b", "c"], ["c", "a"])).toEqual(["a", "c"]);
});

test("appsText", () => {
  expect([appsText(null), appsText([]), appsText(["a"]), appsText(["a", "b"])]).toEqual(["All apps", "No apps", "1 app", "2 apps"]);
});

test("profileInitial", () => {
  expect([profileInitial("mia"), profileInitial("  "), profileInitial("Élodie")]).toEqual(["M", "?", "É"]);
});
