import { readFileSync } from "node:fs";
import { expect, test, type Browser, type Page } from "@playwright/test";

// System › Access for an admin (#334), against a controller of its own:
// making an admin can't be undone, and the other tests need one without.
// One test: each step builds on the devices the one before it paired.

const WRONG_CODE = "no pending pairing request has that code";

/** A browser nobody has approved: its page and the code its login screen shows. */
async function newBrowser(browser: Browser, baseURL: string): Promise<{ page: Page; code: string }> {
  const context = await browser.newContext({ baseURL });
  const page = await context.newPage();
  await page.goto("/");
  const code = (await page.locator("#login-code").innerText()).replace(/\D/g, "");
  expect(code).toHaveLength(6);
  return { page, code };
}

const whoAmI = async (page: Page) => (await page.request.get("/api/auth/status")).json();

test("an admin approves another admin, adds a profile and makes its device an admin", async ({ page, browser, request, baseURL }) => {
  test.setTimeout(180_000);
  const key = readFileSync(process.env.SIM_ADMIN_KEY!, "utf8").trim();
  const access = async () => {
    await page.getByRole("tab", { name: "System" }).locator("visible=true").first().click();
    await page.getByRole("tab", { name: "Access" }).click();
  };

  // The first admin comes from the robot: the dashboard shows a code, the local key approves it.
  await page.goto("/");
  await access();
  await page.getByRole("button", { name: "Make this browser the admin" }).click();
  const mine = (await page.locator("#login-code").innerText()).replace(/\D/g, "");
  const approved = await request.post("/api/auth/approve", {
    headers: { "X-Reachy-Bridge-Key": key },
    data: { code: mine, role: "admin", name: "Laptop", person: "Sam" },
  });
  expect(approved.status()).toBe(200);

  // Only admin devices: a plain device list, nobody waiting.
  const list = page.locator("#access-panel");
  await expect(list.getByRole("heading", { name: "Devices" })).toBeVisible();
  const row = (name: string) => list.getByRole("listitem").filter({ hasText: name });
  await expect(row("Laptop")).toContainText("This browser");
  await expect(row("Laptop")).toContainText("Admin");
  // The last admin device has no button the controller would refuse.
  await expect(row("Laptop").getByRole("button")).toHaveCount(0);
  await expect(list.getByRole("button", { name: "Add profile" })).toBeVisible();
  const waiting = page.getByRole("form", { name: "Waiting for approval" });
  await expect(waiting).toBeHidden();

  // A second browser asks: it shows up by address, never with its code.
  const phone = await newBrowser(browser, baseURL!);
  await expect(waiting).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("img", { name: "a device is waiting" })).toBeVisible();
  await expect(waiting).toContainText("127.0.0.1");
  const shown = await page.locator("body").innerText();
  expect(shown).not.toContain(phone.code);
  expect(shown).not.toContain(`${phone.code.slice(0, 3)} ${phone.code.slice(3)}`);
  const asAdmin = waiting.getByRole("button", { name: "Approve as Admin" });
  await expect(asAdmin).toBeDisabled();

  // A wrong code approves nothing; the right one makes it an admin of the same person.
  const code = waiting.getByRole("textbox", { name: "Code on the waiting device" });
  await code.fill(phone.code === "000000" ? "000001" : "000000");
  page.once("dialog", (dialog) => void dialog.accept());
  await asAdmin.click();
  await expect(list.getByRole("alert")).toContainText(WRONG_CODE);
  expect((await whoAmI(phone.page)).authenticated).toBe(false);
  // Approve as Admin asks first: declined, the code isn't sent.
  await code.fill(phone.code);
  await waiting.getByRole("textbox", { name: "Device name (optional)" }).fill("Phone");
  let question = "";
  page.once("dialog", (dialog) => {
    question = dialog.message();
    void dialog.dismiss();
  });
  await asAdmin.click();
  expect(question).toContain("Approve Phone as an admin?");
  await expect(code).toHaveValue(phone.code);
  expect((await whoAmI(phone.page)).authenticated).toBe(false);
  page.once("dialog", (dialog) => void dialog.accept());
  await asAdmin.click();
  // The phone logs in a moment after the approval, and only then is a device:
  // the list shows it by itself, whenever that is.
  await expect(phone.page.getByRole("tab", { name: "System" }).locator("visible=true").first()).toBeVisible({ timeout: 15_000 });
  await expect.poll(async () => (await whoAmI(phone.page)).role).toBe("admin");
  expect((await whoAmI(phone.page)).person).toBe("Sam");
  await expect(row("Phone")).toContainText("Admin");
  await expect(waiting).toBeHidden();
  // With two admins, either can be revoked.
  await expect(row("Laptop").getByRole("button", { name: "Log out Laptop" })).toBeVisible();
  await expect(row("Phone").getByRole("button", { name: "Revoke Phone" })).toBeVisible();

  // A third browser, for someone new: add the profile, then approve for it.
  const ipad = await newBrowser(browser, baseURL!);
  await expect(waiting).toBeVisible({ timeout: 15_000 });
  await waiting.getByRole("button", { name: "New profile" }).click();
  await page.getByRole("textbox", { name: "Name" }).fill("Mia");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await page.getByRole("button", { name: "Done" }).click();
  await expect(list.getByRole("heading", { name: "Profiles" })).toBeVisible();
  await waiting.getByRole("textbox", { name: "Code on the waiting device" }).fill(ipad.code);
  await waiting.getByRole("textbox", { name: "Device name (optional)" }).fill("iPad");
  await waiting.getByRole("button", { name: "Mia" }).click();
  await expect.poll(async () => (await whoAmI(ipad.page)).role, { timeout: 15_000 }).toBe("member");
  expect((await whoAmI(ipad.page)).person).toBe("Mia");
  await expect(row("iPad")).toBeVisible();
  // A new profile starts limited, and System is the admin's only.
  expect((await ipad.page.request.get("/api/logs/sources")).status()).toBe(403);

  // Make admin asks first, and says what it lifts.
  const makeAdmin = row("iPad").getByRole("button", { name: "Make iPad an admin" });
  let asked = "";
  page.once("dialog", (dialog) => {
    asked = dialog.message();
    void dialog.dismiss();
  });
  await makeAdmin.click();
  expect(asked).toContain("Make iPad an admin?");
  expect(asked).toContain("Mia's profile stops limiting");
  expect((await whoAmI(ipad.page)).role).toBe("member");
  page.once("dialog", (dialog) => void dialog.accept());
  await makeAdmin.click();
  await expect.poll(async () => (await whoAmI(ipad.page)).role).toBe("admin");
  expect((await ipad.page.request.get("/api/logs/sources")).status()).toBe(200);
  await expect(makeAdmin).toBeHidden();

  // Away from Access the list stops asking: a page that isn't showing it has to go quiet,
  // which is how a hidden page's hold on the robot ends. The waiting poll, which carries on
  // while System shows, is the clock: two of them are two of the list's intervals.
  const path = (url: string) => new URL(url).pathname;
  const waitingPoll = () => page.waitForRequest((r) => path(r.url()) === "/api/auth/pending");
  const listReads: string[] = [];
  const onRequest = (r: { url(): string }) => {
    if (/^\/api\/auth\/(devices|profiles)$/.test(path(r.url()))) listReads.push(path(r.url()));
  };
  await page.getByRole("tab", { name: "Settings", exact: true }).click();
  await waitingPoll(); // a read sent just before the switch has gone out by now
  page.on("request", onRequest);
  await waitingPoll();
  await waitingPoll();
  expect(listReads).toEqual([]);
  // Back on Access it asks again at once.
  await page.getByRole("tab", { name: "Access" }).click();
  await expect.poll(() => listReads.length).toBeGreaterThan(0);
  page.off("request", onRequest);

  // The list fits a phone.
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(row("iPad")).toBeVisible();
  // Polled: the tab bars' sliding selection moves to its new place after the resize.
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
});
