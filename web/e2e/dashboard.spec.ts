import { expect, test } from "@playwright/test";
import { TABS, open, setPower, tab, watchErrors } from "./helpers.ts";

// What a person does with the dashboard, against the simulated robot. Only
// the sim moves.

test.beforeEach(async ({ page }) => {
  // Every test starts with the robot off, whatever ran before it.
  await open(page);
  await setPower(page, false);
});

test.afterEach(async ({ page }) => {
  await setPower(page, false).catch(() => {});
});

test("loads without errors and keeps the tab across a reload", async ({ page }) => {
  const errors = watchErrors(page);
  await open(page);
  for (const name of TABS) {
    await tab(page, name);
    await expect(page.getByRole("tab", { name }).locator("visible=true").first()).toHaveAttribute("aria-selected", "true");
  }
  await tab(page, "Apps");
  await page.reload();
  await expect(page.getByRole("tab", { name: "Apps" }).locator("visible=true").first()).toHaveAttribute("aria-selected", "true");
  expect(errors).toEqual([]);
});

test("System's sections switch and keep their place across a reload", async ({ page }) => {
  await tab(page, "System");
  const section = (name: string) => page.getByRole("tab", { name });
  const simulation = page.getByRole("switch", { name: "Simulation" });
  await expect(section("Settings")).toHaveAttribute("aria-selected", "true");
  await expect(simulation).toBeVisible();
  await expect(page.getByRole("link", { name: "User manual" })).toBeVisible();
  await section("Access").click();
  await expect(page.getByRole("heading", { name: "Devices" })).toBeVisible();
  await expect(simulation).toBeHidden();
  await section("Diagnostics").click();
  await expect(page.getByRole("heading", { name: "Board" })).toBeVisible();
  await expect(page.getByRole("log")).toBeVisible();
  await page.reload();
  await expect(section("Diagnostics")).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("log")).toBeVisible();
});

test("the robot's controls follow its power", async ({ page }) => {
  await expect(page.getByRole("button", { name: "Power on" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Play" })).toBeDisabled();
  await tab(page, "Apps");
  await expect(page.getByRole("button", { name: "Start" }).first()).toBeDisabled();
  await setPower(page, true);
  await expect(page.getByRole("button", { name: "Start" }).first()).toBeEnabled();
  await tab(page, "Robot");
  await expect(page.getByRole("button", { name: "Power on" })).toHaveCount(0);
  await setPower(page, false);
  await expect(page.getByRole("button", { name: "Power on" })).toBeVisible();
});

test("an expression plays on the robot", async ({ page, request }) => {
  // The move lists come from Hugging Face through the daemon.
  const list = await request.get("/api/move/recorded-move-datasets/list/pollen-robotics/reachy-mini-emotions-library");
  test.skip(!list.ok() && !process.env.CI, "the emotions dataset isn't available (no Hugging Face access)");
  await open(page);
  await setPower(page, true);
  const play = page.getByRole("button", { name: "Play" });
  await expect(play).toBeEnabled();
  await play.click();
  // While it plays, Play turns busy, then becomes Cancel.
  await expect(page.getByRole("button", { name: "Cancel" }).or(page.locator('button[aria-busy="true"]'))).toBeVisible();
});

test("an app starts, shows its UI and stops", async ({ page }) => {
  test.setTimeout(180_000);
  await setPower(page, true);
  await tab(page, "Apps");
  const row = page.getByRole("listitem").filter({ has: page.getByRole("button", { name: /^Sim Ui App/ }) });
  await row.getByRole("button", { name: "Start" }).click();
  await expect(row.getByRole("button", { name: /^Sim Ui App Running/ })).toBeVisible({ timeout: 60_000 });
  // Starting may open the app's view by itself; otherwise open it.
  const view = page.getByRole("dialog", { name: "Sim Ui App interface" });
  const openButton = row.getByRole("button", { name: "Open" });
  await expect(view.or(openButton).first()).toBeVisible();
  if (!(await view.isVisible())) await openButton.click();
  await expect(view.frameLocator("iframe").locator("body")).toContainText(/\S/, { timeout: 60_000 });
  await view.getByRole("button", { name: "Stop" }).click();
  await expect(view).toBeHidden({ timeout: 60_000 });
  await expect(row.getByRole("button", { name: /^Sim Ui App Not running/ })).toBeVisible({ timeout: 60_000 });
});

test("the favicon, home-screen icon and manifest icons load", async ({ page, request }) => {
  await open(page);
  const links = await page.locator('head link[rel="icon"], head link[rel="apple-touch-icon"]').evaluateAll((els) =>
    els.map((el) => (el as HTMLLinkElement).href),
  );
  expect(links.length).toBeGreaterThanOrEqual(2);
  const manifestUrl = await page.locator('head link[rel="manifest"]').getAttribute("href");
  const manifest = await request.get(manifestUrl ?? "");
  expect(manifest.ok()).toBe(true);
  expect(manifest.headers()["content-type"]).toContain("manifest+json");
  const { icons } = (await manifest.json()) as { icons: { src: string; purpose: string }[] };
  expect(icons.map((i) => i.purpose).sort()).toEqual(["any", "any", "maskable"]);
  for (const url of [...links, ...icons.map((i) => i.src)]) {
    const res = await request.get(url);
    expect(res.ok(), url).toBe(true);
    expect(res.headers()["content-type"], url).toMatch(/^image\//);
  }
});
