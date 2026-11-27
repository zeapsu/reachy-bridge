import { expect, type Page } from "@playwright/test";

export const TABS = ["Robot", "Apps", "System"] as const;
export type TabName = (typeof TABS)[number];

/** The dashboard, loaded, with its first status poll answered. */
export async function open(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("tab", { name: "Robot" }).first()).toBeVisible();
  await expect(power(page)).toBeVisible();
}

export async function tab(page: Page, name: TabName): Promise<void> {
  // Desktop shows the tabs at the top, a phone at the bottom: whichever shows.
  await page.getByRole("tab", { name }).locator("visible=true").first().click();
}

/** Every view the audits visit: each tab, and each of System's sections (#334). */
export const VIEWS = [
  { tab: "Robot" },
  { tab: "Apps" },
  { tab: "System", section: "Settings" },
  { tab: "System", section: "Access" },
  { tab: "System", section: "Diagnostics" },
] as const satisfies readonly { tab: TabName; section?: string }[];
export type View = (typeof VIEWS)[number];

export const viewName = (v: View): string => ("section" in v ? `${v.tab} › ${v.section}` : v.tab);

export async function view(page: Page, v: View): Promise<void> {
  await tab(page, v.tab);
  if ("section" in v) await page.getByRole("tab", { name: v.section }).click();
}

export function power(page: Page) {
  return page.getByRole("button", { name: /^Switch robot (on|off)$/ });
}

/** Power the simulated robot on or off through the dashboard and wait for it. */
export async function setPower(page: Page, on: boolean): Promise<void> {
  const want = on ? "Switch robot off" : "Switch robot on";
  if (await page.getByRole("button", { name: want }).isVisible()) return;
  await power(page).click();
  await expect(page.getByRole("button", { name: want })).toBeVisible({ timeout: 60_000 });
}

/**
 * Errors the page threw or logged, and requests that never got an answer.
 * Error statuses aren't counted: the sim answers some with 404 or 503 (no
 * setup, volume or platform), and the dashboard shows those itself.
 */
export function watchErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(`page error: ${error.message}`));
  page.on("requestfailed", (request) => {
    // Leaving or reloading the page aborts its open requests.
    if (request.failure()?.errorText !== "net::ERR_ABORTED") errors.push(`request failed: ${request.url()} ${request.failure()?.errorText}`);
  });
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) {
      errors.push(`console: ${message.text()}`);
    }
  });
  return errors;
}
