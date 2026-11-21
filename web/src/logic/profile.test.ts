import { describe, expect, test } from "bun:test";
import { allowedApps, appsText, profileInitial, rights } from "./profile.ts";

const mia = { name: "Mia", admin: false, shared: false, apps: ["dance"], can: { power: false, drive: true, voice: false, camera: false, controls: false } };

describe("rights", () => {
  test("no profile (no admin mode) allows everything", () => {
    const r = rights(null);
    expect([r.family, r.system, r.controls, r.can.power, r.apps, r.picking]).toEqual([false, true, true, true, null, false]);
  });
  test("the admin has everything", () => {
    const r = rights({ ...mia, admin: true });
    expect([r.family, r.system, r.controls, r.can.camera, r.apps]).toEqual([true, true, true, true, null]);
  });
  test("a limited profile loses System, and Controls with its switch", () => {
    const r = rights(mia);
    expect([r.system, r.controls, r.can.power, r.can.drive, r.apps]).toEqual([false, false, false, true, ["dance"]]);
  });
  test("Controls on keeps the pane but not System", () => {
    const r = rights({ ...mia, can: { ...mia.can, controls: true } });
    expect([r.system, r.controls]).toEqual([false, true]);
  });
  test("a shared device without a pick is picking", () => {
    expect(rights({ ...mia, name: null, shared: true }).picking).toBe(true);
    expect(rights({ ...mia, shared: true }).picking).toBe(false);
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
