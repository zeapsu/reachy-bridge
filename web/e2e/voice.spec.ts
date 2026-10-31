import { mkdirSync, writeFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import { HEARD, SILENT, type Output, type Tone, heardIn, heardInBrowser, mark, tapIncomingAudio } from "./audio.ts";
import { open, setPower } from "./helpers.ts";

// The audio traffic, end to end (#219): the dashboard's voice switches, the
// controller, the audio gateway and WebRTC between it and this browser. Each
// source plays its own tone (tools/sim/run.py --audio): the robot's mic, what
// the robot plays, and this browser's mic. For each set of switches, every
// tone must reach exactly the outputs it's routed to: this browser, the
// robot's speaker, or what its apps hear as its mic.
//
// The ALSA side (the speaker and mic tee, the mic mixer, the speaker mute)
// needs a loopback sound card, which CI runners don't have.

test.skip(!process.env.SIM_AUDIO, "needs the audio gateway (tools/sim/run.py --audio)");

type Route = { listen: boolean; hear: boolean; talk: boolean; apps: boolean };
const SWITCHES: Record<keyof Route, string> = {
  listen: "Hear Reachy's surroundings (its mic)",
  hear: "Hear what Reachy says (its speaker)",
  talk: "Talk through the robot",
  apps: "Talk to Reachy's apps (into its mic)",
};
// Every measurement, for the CI job summary (e2e/summary.ts).
const measured: { route: Route; heard: Record<string, Record<Tone, number>> }[] = [];
test.afterAll(() => {
  mkdirSync("test-results", { recursive: true });
  writeFileSync("test-results/voice-traffic.json", JSON.stringify(measured, null, 2));
});

// Time for a switch to reach the gateway and its pipeline to settle.
const SETTLE_MS = 1500;
const MEASURE_MS = 1500;

function sw(page: Page, key: keyof Route) {
  return page.getByRole("switch", { name: SWITCHES[key] });
}

async function voicePane(page: Page): Promise<void> {
  await page.getByRole("group", { name: "Robot controls" }).getByRole("button", { name: "Voice" }).click();
  await expect(page.getByRole("heading", { name: "Listen and talk" })).toBeVisible();
}

/** Set the switches to `route`, one at a time, as a person would. */
async function setRoute(page: Page, route: Route): Promise<void> {
  for (const key of Object.keys(SWITCHES) as (keyof Route)[]) {
    const s = sw(page, key);
    if ((await s.getAttribute("aria-checked")) !== String(route[key])) {
      await expect(s).toBeEnabled();
      await s.click();
      await expect(s).toHaveAttribute("aria-checked", String(route[key]));
    }
  }
}

/** Which tones each output should carry for `route`. */
function expected(route: Route): Record<"browser" | Output, Tone[]> {
  return {
    browser: [...(route.listen ? ["mic" as const] : []), ...(route.hear ? ["robot" as const] : [])],
    speaker: route.talk ? ["browser"] : [],
    apps: route.apps ? ["browser"] : [],
  };
}

/** Measure every output at once and check each tone is there or not, as `route` says. */
async function checkTraffic(page: Page, route: Route): Promise<void> {
  await page.waitForTimeout(SETTLE_MS);
  const from = { speaker: mark("speaker"), apps: mark("apps") };
  const browser = await heardInBrowser(page, MEASURE_MS);
  const heard = { browser, speaker: heardIn("speaker", from.speaker).tones, apps: heardIn("apps", from.apps).tones };
  const want = expected(route);
  const report = JSON.stringify({ route, heard }, (_k, v) => (typeof v === "number" ? Number(v.toFixed(4)) : v));
  measured.push({ route, heard });
  for (const output of ["browser", "speaker", "apps"] as const) {
    for (const tone of ["mic", "robot", "browser"] as const) {
      const level = heard[output][tone];
      if (want[output].includes(tone)) expect(level, `${tone} tone should reach ${output}: ${report}`).toBeGreaterThan(HEARD);
      else expect(level, `${tone} tone shouldn't reach ${output}: ${report}`).toBeLessThan(SILENT);
    }
  }
}

test.beforeEach(async ({ page }) => {
  await tapIncomingAudio(page);
  await open(page);
  // Listening and talking go with the robot: it has to be on.
  await setPower(page, true);
  await voicePane(page);
});

test.afterEach(async ({ page }) => {
  await page.locator("#voice-stop").click({ timeout: 5000 }).catch(() => {});
  await setPower(page, false).catch(() => {});
});

test("each switch sends its sound where it says, and nowhere else", async ({ page }) => {
  test.setTimeout(180_000);
  const start: Route = { listen: true, hear: false, talk: false, apps: false };
  await setRoute(page, start);
  await page.locator("#voice-start").click();
  await expect(page.locator("#voice-panel .chip")).toHaveText("live", { timeout: 30_000 });
  await checkTraffic(page, start);
  // Then each switch on its own and in combination, without reconnecting.
  const steps: Route[] = [
    { listen: true, hear: true, talk: false, apps: false },
    { listen: false, hear: true, talk: false, apps: false },
    { listen: false, hear: false, talk: true, apps: false },
    { listen: false, hear: false, talk: true, apps: true },
    { listen: false, hear: false, talk: false, apps: true },
    { listen: true, hear: true, talk: true, apps: true },
    { listen: false, hear: false, talk: false, apps: false },
  ];
  for (const route of steps) {
    await setRoute(page, route);
    await expect(page.locator("#voice-panel .chip")).toHaveText("live");
    await checkTraffic(page, route);
  }
});

test("stopping ends the sound everywhere", async ({ page }) => {
  await setRoute(page, { listen: true, hear: true, talk: true, apps: true });
  await page.locator("#voice-start").click();
  await expect(page.locator("#voice-panel .chip")).toHaveText("live", { timeout: 30_000 });
  await page.locator("#voice-stop").click();
  await expect(page.locator("#voice-panel .chip")).toHaveText("off");
  await page.waitForTimeout(SETTLE_MS);
  const from = { speaker: mark("speaker"), apps: mark("apps") };
  await page.waitForTimeout(MEASURE_MS);
  // The gateway's pipeline is gone: nothing more reaches the robot.
  expect(heardIn("speaker", from.speaker).seconds).toBe(0);
  expect(heardIn("apps", from.apps).seconds).toBe(0);
  expect((await page.request.get("/api/audio").then((r) => r.json())).session).toBeNull();
});

test("turning the robot off ends the session", async ({ page }) => {
  await setRoute(page, { listen: true, hear: false, talk: true, apps: false });
  await page.locator("#voice-start").click();
  await expect(page.locator("#voice-panel .chip")).toHaveText("live", { timeout: 30_000 });
  await setPower(page, false);
  await expect(page.locator("#voice-panel .chip")).toHaveText("off", { timeout: 30_000 });
  await expect.poll(async () => (await page.request.get("/api/audio").then((r) => r.json())).session, { timeout: 30_000 }).toBeNull();
});
