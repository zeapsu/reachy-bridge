import { expect, test, type Page } from "@playwright/test";
import { VIEWS, open, power, view, viewName } from "./helpers.ts";
import { ratchet } from "./ratchet.ts";

// Layout and copy checks on every tab and each of System's sections. Horizontal overflow fails outright;
// the rest are counted against e2e/baselines/ so they can only go down:
// - small-target: a control smaller than 24×24 px (WCAG 2.5.8), easy to miss
//   with a thumb;
// - long-text: visible text over 12 words. The dashboard explains state with
//   its controls, not with sentences; explanations belong in the docs.
// Load time is recorded for the job summary, not checked.

const MIN_TARGET = 24;
const MAX_WORDS = 12;

type Finding = { tab: string; what: string; detail: string };

async function audit(page: Page, name: string): Promise<{ overflow: number; findings: Finding[] }> {
  return page.evaluate(
    ({ name, MIN_TARGET, MAX_WORDS }) => {
      const visible = (el: Element) => {
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      };
      const label = (el: Element) =>
        (el.getAttribute("aria-label") || el.textContent || el.getAttribute("title") || el.tagName).trim().slice(0, 60);
      const findings: { tab: string; what: string; detail: string }[] = [];
      const controls = document.querySelectorAll(
        'button, a[href], input:not([type="hidden"]), select, textarea, [role="button"], [role="switch"], [role="tab"], [role="slider"]',
      );
      for (const el of controls) {
        if (!visible(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width < MIN_TARGET || r.height < MIN_TARGET) {
          findings.push({ tab: name, what: "small-target", detail: `${label(el)} (${Math.round(r.width)}×${Math.round(r.height)})` });
        }
      }
      const seen = new Set<string>();
      for (const el of document.querySelectorAll("p, li, span, small, label, [role='status'], [role='alert'], h1, h2, h3, div")) {
        // Logs are output, not copy.
        if (!visible(el) || el.closest("pre, code, [role='log']")) continue;
        // Only an element's own text, so a container isn't counted with its children.
        const own = [...el.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE).map((n) => n.textContent ?? "").join(" ").trim();
        const words = own.split(/\s+/).filter(Boolean).length;
        if (words > MAX_WORDS && !seen.has(own)) {
          seen.add(own);
          findings.push({ tab: name, what: "long-text", detail: `${words} words: ${own.slice(0, 120)}` });
        }
      }
      const overflow = document.documentElement.scrollWidth - document.documentElement.clientWidth;
      return { overflow, findings };
    },
    { name, MIN_TARGET, MAX_WORDS },
  );
}

test("layout and copy", async ({ page }, info) => {
  const started = Date.now();
  await open(page);
  await expect(power(page)).toBeEnabled();
  const loadMs = Date.now() - started;

  const findings: Finding[] = [];
  const overflows: string[] = [];
  for (const v of VIEWS) {
    const name = viewName(v);
    await view(page, v);
    const result = await audit(page, name);
    if (result.overflow > 0) overflows.push(`${name}: ${result.overflow}px wider than the screen`);
    findings.push(...result.findings);
  }
  const counts: Record<string, number> = {};
  for (const f of findings) counts[f.what] = (counts[f.what] ?? 0) + 1;
  ratchet(info, "ux", counts, { loadMs, findings });
  expect(overflows, "pages must not scroll sideways").toEqual([]);
});
