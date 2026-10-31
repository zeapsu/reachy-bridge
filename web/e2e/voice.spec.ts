import { mkdirSync, writeFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import { HEARD, SILENT, type Output, type Tone, gatewaySession, heardIn, heardInBrowser, mark, tapIncomingAudio } from "./audio.ts";
import { open, setPower } from "./helpers.ts";

// The audio traffic, end to end (#219): the dashboard's voice switches, the
// controller, the audio gateway and WebRTC between it and this browser. Each
// source plays its own tone (tools/sim/run.py --audio): the robot's mic, what
// the robot plays, and this browser's mic. For each set of switches, every
// tone must reach exactly the outputs it's routed to: this browser, the
// robot's speaker, or what its apps hear as its mic.
//
// The ALSA side (the speaker and mic tee, the mic mixer, the speaker mute)
// needs a loopback sound card, which CI runners don't have: PRs that change
// audio code are labelled for a check on a robot.

test.skip(!process.env.SIM_AUDIO, "needs the audio gateway (tools/sim/run.py --audio)");

type Route = { listen: boolean; hear: boolean; talk: boolean; apps: boolean };
const SWITCHES: Record<keyof Route, string> = {
  listen: "Hear Reachy's surroundings (its mic)",
  hear: "Hear what Reachy says (its speaker)",
  talk: "Talk through the robot",
  apps: "Talk to Reachy's apps (into its mic)",
};
// Time for the gateway's pipeline to settle after it applied a switch.
const SETTLE_MS = 1000;
const MEASURE_MS = 1500;

// Every measurement, one file per test, for the CI job summary (e2e/summary.ts).
let measured: { route: Route; heard: Record<string, Record<Tone, number>> }[] = [];

function sw(page: Page, key: keyof Route) {
  return page.getByRole("switch", { name: SWITCHES[key] });
}

const chip = (page: Page) => page.locator("#voice-panel .chip");

async function voicePane(page: Page): Promise<void> {
  await page.getByRole("group", { name: "Robot controls" }).getByRole("button", { name: "Voice" }).click();
  await expect(page.getByRole("heading", { name: "Listen and talk" })).toBeVisible();
}

/** Set the switches to `route`, one at a time as a person would, each applied before the next. */
async function setRoute(page: Page, route: Route): Promise<void> {
  for (const key of Object.keys(SWITCHES) as (keyof Route)[]) {
    const s = sw(page, key);
    if ((await s.getAttribute("aria-checked")) !== String(route[key])) {
      await expect(s).toBeEnabled();
      await s.click();
      await expect(s).toHaveAttribute("aria-checked", String(route[key]));
    }
  }
  // Busy until the controller answered the last switch.
  for (const key of Object.keys(SWITCHES) as (keyof Route)[]) await expect(sw(page, key)).toBeEnabled();
}

async function start(page: Page, route: Route): Promise<void> {
  await setRoute(page, route);
  await page.locator("#voice-start").click();
  await expect(chip(page)).toHaveText("live", { timeout: 30_000 });
}

/** Which tones each output should carry for `route`. */
function expected(route: Route): Record<"browser" | Output, Tone[]> {
  return {
    browser: [...(route.listen ? ["mic" as const] : []), ...(route.hear ? ["robot" as const] : [])],
    speaker: route.talk ? ["browser"] : [],
    apps: route.apps ? ["browser"] : [],
  };
}

/**
 * Measure every output at once and check each tone is there or not, as
 * `route` says. The absences only count if the connection was alive
 * throughout: packets came in, and the gateway has this route.
 */
async function checkTraffic(page: Page, route: Route): Promise<void> {
  await page.waitForTimeout(SETTLE_MS);
  const from = { speaker: mark("speaker"), apps: mark("apps") };
  const sentBefore = Number((await gatewaySession())?.packets_in ?? 0);
  const browser = await heardInBrowser(page, MEASURE_MS);
  const heard = { browser: browser.tones, speaker: heardIn("speaker", from.speaker).tones, apps: heardIn("apps", from.apps).tones };
  const session = await gatewaySession();
  const report = JSON.stringify({ route, heard }, (_k, v) => (typeof v === "number" ? Number(v.toFixed(4)) : v));
  measured.push({ route, heard });

  expect(browser.state, "this browser's WebAudio ran").toBe("running");
  // Blocks are read on timers, which run late on a busy machine: half is plenty.
  expect(browser.seconds, "this browser recorded").toBeGreaterThan((0.5 * MEASURE_MS) / 1000);
  expect(browser.packets, "audio packets kept coming from the gateway").toBeGreaterThan(0);
  expect(session, "the gateway has this route, connected").toMatchObject({ ...route, connected: true });
  if (route.talk || route.apps) {
    expect(Number(session?.packets_in) - sentBefore, "this browser's voice reached the gateway").toBeGreaterThan(0);
  }
  await expect(chip(page)).toHaveText("live");

  const want = expected(route);
  for (const output of ["browser", "speaker", "apps"] as const) {
    for (const tone of ["mic", "robot", "browser"] as const) {
      const level = heard[output][tone];
      if (want[output].includes(tone)) expect(level, `${tone} tone should reach ${output}: ${report}`).toBeGreaterThan(HEARD);
      else expect(level, `${tone} tone shouldn't reach ${output}: ${report}`).toBeLessThan(SILENT);
    }
  }
}

/**
 * The gateway closes its session: the robot's mic and speaker are free again.
 * The controller's DELETE does it in seconds; well short of the 30 s after
 * which the gateway would end a dropped session by itself.
 */
async function gatewayEnds(): Promise<void> {
  await expect.poll(gatewaySession, { timeout: 15_000, message: "the gateway ended the session" }).toBeNull();
}

test.beforeEach(async ({ page }) => {
  await tapIncomingAudio(page);
  await open(page);
  // Listening and talking go with the robot: it has to be on.
  await setPower(page, true);
  await voicePane(page);
});

test.afterEach(async ({ page }, info) => {
  if (measured.length) {
    mkdirSync("test-results/voice-traffic", { recursive: true });
    writeFileSync(`test-results/voice-traffic/${info.testId}.json`, JSON.stringify({ title: info.title, steps: measured }, null, 2));
    measured = [];
  }
  await page.locator("#voice-stop").click({ timeout: 5000 }).catch(() => {});
  await setPower(page, false).catch(() => {});
});

test("each switch sends its sound where it says, and nowhere else", async ({ page }) => {
  test.setTimeout(180_000);
  const first: Route = { listen: true, hear: false, talk: false, apps: false };
  await start(page, first);
  await checkTraffic(page, first);
  // Then each switch on its own and in combination, without reconnecting.
  // Talk and apps each get steps with the other on, so each valve is seen
  // closed while this browser's voice is coming in.
  const steps: Route[] = [
    { listen: true, hear: true, talk: false, apps: false },
    { listen: false, hear: true, talk: false, apps: false },
    { listen: false, hear: false, talk: true, apps: false },
    { listen: false, hear: false, talk: true, apps: true },
    { listen: false, hear: false, talk: false, apps: true },
    { listen: true, hear: true, talk: true, apps: true },
    { listen: false, hear: true, talk: false, apps: true },
    { listen: true, hear: false, talk: true, apps: false },
  ];
  for (const route of steps) {
    await setRoute(page, route);
    await checkTraffic(page, route);
  }
});

test("stopping ends the sound everywhere", async ({ page }) => {
  const all: Route = { listen: true, hear: true, talk: true, apps: true };
  await start(page, all);
  // It carries sound first, so the silence after Stop means something.
  await checkTraffic(page, all);
  await page.locator("#voice-stop").click();
  await expect(chip(page)).toHaveText("off");
  await gatewayEnds();
  const from = { speaker: mark("speaker"), apps: mark("apps") };
  await page.waitForTimeout(MEASURE_MS);
  expect(heardIn("speaker", from.speaker).seconds).toBe(0);
  expect(heardIn("apps", from.apps).seconds).toBe(0);
  expect((await page.request.get("/api/audio").then((r) => r.json())).session).toBeNull();
});

test("turning the robot off ends the session", async ({ page }) => {
  await start(page, { listen: true, hear: false, talk: true, apps: false });
  await setPower(page, false);
  await expect(chip(page)).toHaveText("off", { timeout: 30_000 });
  await gatewayEnds();
  await expect.poll(async () => (await page.request.get("/api/audio").then((r) => r.json())).session, { timeout: 30_000 }).toBeNull();
});
