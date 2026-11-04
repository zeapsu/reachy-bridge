import { expect, test, type Page } from "@playwright/test";
import { silentMicFile } from "./audio.ts";
import { open, setPower } from "./helpers.ts";

// Talk with a mic that sends only silence (a dead or wrong input):
// the Voice pane offers the browser's other inputs. Its own file, because the
// fake mic is a browser launch option.

test.skip(!process.env.SIM_AUDIO, "needs the audio gateway (tools/sim/run.py --audio)");

test.use({
  launchOptions: {
    args: [
      "--use-fake-ui-for-media-stream",
      "--use-fake-device-for-media-stream",
      `--use-file-for-fake-audio-capture=${silentMicFile()}`,
      "--autoplay-policy=no-user-gesture-required",
    ],
  },
});

const talk = (page: Page) => page.getByRole("switch", { name: "Talk through the robot" });
const picker = (page: Page) => page.getByRole("combobox", { name: "Microphone" });
const session = async (page: Page) => (await page.request.get("/api/audio").then((r) => r.json())).session?.ref;

test.beforeEach(async ({ page }) => {
  await open(page);
  await setPower(page, true);
  await page.getByRole("group", { name: "Robot controls" }).getByRole("button", { name: "Voice" }).click();
});

test.afterEach(async ({ page }) => {
  await page.locator("#voice-stop").click({ timeout: 5000 }).catch(() => {});
  await setPower(page, false).catch(() => {});
});

test("a silent mic gets the other inputs offered, and switching keeps the session", async ({ page }) => {
  await talk(page).click();
  await page.locator("#voice-start").click();
  await expect(page.locator("#voice-panel .chip")).toHaveText("live", { timeout: 30_000 });
  await expect(picker(page)).toBeVisible({ timeout: 10_000 });
  const values = await picker(page)
    .locator("option")
    .evaluateAll((options) => options.map((o) => (o as HTMLOptionElement).value));
  const current = await picker(page).inputValue();
  const other = values.find((v) => v !== current);
  expect(other, "the fake device has more than one input").toBeTruthy();
  const before = await session(page);
  await picker(page).selectOption(other as string);
  await expect(picker(page)).toHaveValue(other as string);
  // Still silent, so the picker stays; same session, still live.
  await expect(page.locator("#voice-panel .chip")).toHaveText("live");
  expect(await session(page)).toBe(before);
  // Talking off and on again keeps the picked input.
  await talk(page).click();
  await expect(picker(page)).toHaveCount(0);
  await talk(page).click();
  await expect(picker(page)).toHaveValue(other as string, { timeout: 10_000 });
});
