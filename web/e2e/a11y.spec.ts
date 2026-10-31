import AxeBuilder from "@axe-core/playwright";
import { test } from "@playwright/test";
import { TABS, open, setPower, tab } from "./helpers.ts";
import { ratchet } from "./ratchet.ts";

// axe-core on every tab, with the robot off and on. Violations are counted per
// rule (one per offending element) against e2e/baselines/, so new ones fail
// and existing ones are tracked until they're fixed.

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

for (const power of ["off", "on"] as const) {
  test(`accessibility, robot ${power}`, async ({ page }, info) => {
    await open(page);
    await setPower(page, power === "on");
    const counts: Record<string, number> = {};
    const details: Record<string, unknown[]> = {};
    for (const name of TABS) {
      await tab(page, name);
      // The app UIs are third-party pages; only the dashboard is ours.
      const { violations } = await new AxeBuilder({ page }).withTags(TAGS).exclude("iframe").analyze();
      for (const v of violations) {
        counts[v.id] = (counts[v.id] ?? 0) + v.nodes.length;
        (details[v.id] ??= []).push(
          ...v.nodes.map((node) => ({ tab: name, impact: v.impact, help: v.help, target: node.target.join(" "), html: node.html.slice(0, 200) })),
        );
      }
    }
    ratchet(info, `a11y-${power}`, counts, details);
    await setPower(page, false);
  });
}
