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

/** Errors the page logged, and requests that failed, while `run` ran. */
export function watchErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(`page error: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) {
      errors.push(`console: ${message.text()}`);
    }
  });
  return errors;
}
