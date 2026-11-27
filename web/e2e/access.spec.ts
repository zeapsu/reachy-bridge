import { readFileSync } from "node:fs";
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Browser, type Dialog, type Page, type Route } from "@playwright/test";
import { watchErrors } from "./helpers.ts";

// System › Access for an admin (#334), against a controller of its own:
// admin mode can't be turned off again, and the other tests need a controller without it.
// One test: each step builds on the devices the one before it paired.

const WRONG_CODE = "no pending pairing request has that code";
const LAST_ADMIN = "this is the last admin device";
const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

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

// Desktop shows the tabs at the top, a phone at the bottom: whichever shows.
const tab = (page: Page, name: string) => page.getByRole("tab", { name }).locator("visible=true").first();

/** What the Access section must hold at any size: no axe violation, no control too small to tap, nothing wider than the screen. */
async function auditAccess(page: Page): Promise<void> {
  const { violations } = await new AxeBuilder({ page }).withTags(AXE_TAGS).include("#system-access").analyze();
  expect(violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
  const small = await page.locator("#system-access").evaluate((pane) =>
    [...pane.querySelectorAll("button, input")]
      .map((el) => ({ name: el.getAttribute("aria-label") || el.textContent || "", box: el.getBoundingClientRect() }))
      .filter(({ box }) => box.width > 0 && (box.width < 24 || box.height < 24))
      .map(({ name, box }) => `${name} (${Math.round(box.width)}×${Math.round(box.height)})`),
  );
  expect(small).toEqual([]);
  // Polled: the tab bars' sliding selection moves to its new place after a resize.
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
}

test("an admin approves another admin, adds a profile, makes its device an admin and demotes admins", async ({ page, browser, request, baseURL }) => {
  test.setTimeout(180_000);
  const key = readFileSync(process.env.SIM_ADMIN_KEY!, "utf8").trim();
  const access = async () => {
    await tab(page, "System").click();
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
  // Admin comes last and plain: the profiles and Shared come first.
  await expect(waiting.getByRole("button").last()).toHaveAccessibleName("Approve as Admin");
  await expect(asAdmin).toHaveText(/^\s*Admin\s*$/);
  await expect(asAdmin).not.toHaveClass(/\bprimary\b/);

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
  // With two admins, either can be revoked or demoted.
  await expect(row("Laptop").getByRole("button", { name: "Log out Laptop" })).toBeVisible();
  await expect(row("Phone").getByRole("button", { name: "Revoke Phone" })).toBeVisible();
  await expect(row("Laptop").getByRole("button", { name: "Demote Laptop" })).toBeVisible();
  await expect(row("Phone").getByRole("button", { name: "Demote Phone" })).toBeVisible();

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
  const makeAdmin = row("iPad").getByRole("button", { name: "Make admin: iPad" });
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

  // Demote asks first, and says whose profile applies again. The iPad has System open meanwhile.
  const errors = { laptop: watchErrors(page), ipad: watchErrors(ipad.page) };
  await expect(tab(ipad.page, "System")).toBeEnabled();
  await tab(ipad.page, "System").click();
  await expect(ipad.page.getByRole("region", { name: "System" })).toBeVisible();
  const demote = (name: string) => row(name).getByRole("button", { name: `Demote ${name}` });
  // Control is read again after a yes: the iPad takes it while the question is open, so Demote asks
  // again, now with the warning. Declined, nothing changes.
  const asks: string[] = [];
  const answer = (dialog: Dialog) => {
    asks.push(dialog.message());
    if (asks.length === 1) void ipad.page.request.post("/api/control/take").then(() => dialog.accept());
    else void dialog.dismiss();
  };
  page.on("dialog", answer);
  await demote("iPad").click();
  await expect.poll(() => asks.length).toBe(2);
  page.off("dialog", answer);
  expect(asks[0]).toContain("Demote iPad?");
  expect(asks[0]).not.toContain("is in control");
  expect(asks[1]).toContain("iPad is in control: the robot may move back home.");
  expect((await whoAmI(ipad.page)).role).toBe("admin");
  // The iPad takes control: once this page has seen that, Demote also says the robot may move.
  expect((await ipad.page.request.post("/api/control/take")).status()).toBe(200);
  await expect
    .poll(async () => {
      page.once("dialog", (dialog) => {
        asked = dialog.message();
        void dialog.dismiss();
      });
      await demote("iPad").click();
      return asked;
    })
    .toContain("iPad is in control: the robot may move back home.");
  expect(asked).toContain("Demote iPad?");
  expect(asked).toContain("Mia's profile applies to every device of Mia's again.");
  expect(asked).not.toContain("Only another admin");
  expect((await whoAmI(ipad.page)).role).toBe("admin");
  page.once("dialog", (dialog) => void dialog.accept());
  await demote("iPad").click();
  await expect.poll(async () => (await whoAmI(ipad.page)).role).toBe("member");
  expect((await ipad.page.request.get("/api/logs/sources")).status()).toBe(403);
  // The row offers the way back, and Mia's profile is hers to edit again.
  await expect(makeAdmin).toBeVisible();
  await expect(demote("iPad")).toBeHidden();
  await expect(list.getByRole("button", { name: "Edit Mia" })).toBeVisible();
  // The iPad leaves System by itself.
  await expect(tab(ipad.page, "System")).toBeDisabled();
  await expect(ipad.page.getByRole("region", { name: "System" })).toBeHidden();
  expect(errors.ipad).toEqual([]);

  // A list that is behind: the Phone stopped being an admin after this page last read it, so the
  // Laptop is the last one while its row still offers Demote. The controller refuses, the page
  // says so and reads the list again.
  const LIST = /\/api\/auth\/(devices|profiles)$/;
  const held: Route[] = [];
  let hold = true;
  await page.route(LIST, (route) => (hold ? void held.push(route) : route.continue()));
  // A read held: the one before it has landed, so nothing newer than what shows can.
  await expect.poll(() => held.length).toBeGreaterThan(0);
  const devices: { id: string; name: string }[] = (await (await page.request.get("/api/auth/devices")).json()).devices;
  const phoneRole = `/api/auth/devices/${devices.find((d) => d.name === "Phone")!.id}/role`;
  expect((await page.request.put(phoneRole, { data: { role: "member" } })).status()).toBe(200);
  const before = held.length;
  page.once("dialog", (dialog) => void dialog.accept());
  await demote("Laptop").click();
  await expect(list.getByRole("alert")).toContainText(LAST_ADMIN);
  expect((await whoAmI(page)).role).toBe("admin");
  // The refusal itself reads the list again, not only the next poll.
  await expect.poll(() => held.length).toBeGreaterThan(before);
  hold = false;
  for (const route of held) await route.continue();
  await page.unroute(LIST);
  // The last admin device has no button the controller would refuse.
  await expect(row("Laptop").getByRole("button")).toHaveCount(0);
  await expect(row("Laptop")).toContainText("This browser");
  // The Phone is an admin again for what follows.
  page.once("dialog", (dialog) => void dialog.accept());
  await row("Phone").getByRole("button", { name: "Make admin: Phone" }).click();
  await expect(demote("Phone")).toBeVisible();
  await expect(demote("Laptop")).toBeVisible();
  await auditAccess(page);

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

  // The list fits a phone, Demote and Revoke under the name they belong to.
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(row("iPad")).toBeVisible();
  await expect(demote("Phone")).toBeInViewport();
  await expect(row("Phone").getByRole("button", { name: "Revoke Phone" })).toBeInViewport();
  await auditAccess(page);

  // Demoting this browser says it can't be undone from here. Declined, nothing changes.
  page.once("dialog", (dialog) => {
    asked = dialog.message();
    void dialog.dismiss();
  });
  await demote("Laptop").click();
  expect(asked).toContain("Demote this browser (Laptop)?");
  expect(asked).toContain("Only another admin can make it an admin again.");
  // Sam has another admin device: nobody's profile comes back.
  expect(asked).not.toContain("profile applies");
  expect((await whoAmI(page)).role).toBe("admin");
  // Accepted, the page starts over at once as Sam's member browser: System is closed to it.
  const reloaded = page.waitForEvent("load");
  page.once("dialog", (dialog) => void dialog.accept());
  await demote("Laptop").click();
  await reloaded;
  await expect(tab(page, "System")).toBeDisabled();
  await expect(tab(page, "Robot")).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("region", { name: "System" })).toBeHidden();
  expect((await whoAmI(page)).role).toBe("member");
  expect((await page.request.get("/api/logs/sources")).status()).toBe(403);
  expect(errors.laptop).toEqual([]);
  // The Phone, the admin that is left, sees it as any other browser of Sam's.
  await expect(tab(phone.page, "System")).toBeEnabled();
  await tab(phone.page, "System").click();
  await phone.page.getByRole("tab", { name: "Access" }).click();
  const phoneRow = (name: string) => phone.page.locator("#access-panel").getByRole("listitem").filter({ hasText: name });
  await expect(phoneRow("Laptop").getByRole("button", { name: "Make admin: Laptop" })).toBeVisible();
  await expect(phoneRow("Phone").getByRole("button")).toHaveCount(0);
});
