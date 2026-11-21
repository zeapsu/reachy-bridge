import { expect, test, type Page } from "@playwright/test";
import { silentMicFile } from "./audio.ts";
import { open, setPower } from "./helpers.ts";

// Talk with a mic that sends only silence (a dead or muted input): the
// Voice pane flags the mic row, and another input can be picked. Its own
// file, because the fake mic is a browser launch option.

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

test("a silent mic is flagged, and switching inputs keeps the session", async ({ page }) => {
  await talk(page).click();
  await page.locator("#voice-start").click();
  await expect(page.locator("#voice-panel .chip")).toHaveText("live", { timeout: 30_000 });
  await expect(picker(page)).toBeVisible();
  await expect(page.locator(".mic-row.silent")).toBeVisible({ timeout: 10_000 });
  const values = await picker(page)
    .locator("option")
    .evaluateAll((options) => options.map((o) => (o as HTMLOptionElement).value));
  const current = await picker(page).inputValue();
  const other = values.find((v) => v !== current);
  expect(other, "the fake device has more than one input").toBeTruthy();
  const before = await session(page);
  await picker(page).selectOption(other as string);
  await expect(picker(page)).toHaveValue(other as string);
  // Still silent, so still flagged; same session, still live.
  await expect(page.locator(".mic-row.silent")).toBeVisible();
  await expect(page.locator("#voice-panel .chip")).toHaveText("live");
  expect(await session(page)).toBe(before);
  // Talking off and on again keeps the picked input.
  await talk(page).click();
  await expect(picker(page)).toHaveCount(0);
  await talk(page).click();
  await expect(picker(page)).toHaveValue(other as string, { timeout: 10_000 });
});

test("the mic can be picked before starting", async ({ page }) => {
  await expect(picker(page)).toHaveCount(0);
  await talk(page).click();
  // The browser already may use the mic (the fake one), so it knows the names.
  await expect(picker(page)).toBeVisible();
  const values = await picker(page)
    .locator("option")
    .evaluateAll((options) => options.map((o) => (o as HTMLOptionElement).value));
  const other = values.find((v) => v !== (values[0] ?? ""));
  expect(other, "the fake device has more than one input").toBeTruthy();
  await picker(page).selectOption(other as string);
  await page.locator("#voice-start").click();
  await expect(page.locator("#voice-panel .chip")).toHaveText("live", { timeout: 30_000 });
  // Talking started on the picked input.
  await expect(picker(page)).toHaveValue(other as string);
  // Both Talk chips off: the row goes.
  await talk(page).click();
  await expect(picker(page)).toHaveCount(0);
});
