import AxeBuilder from "@axe-core/playwright";
import { test } from "@playwright/test";
import { VIEWS, open, setPower, view, viewName } from "./helpers.ts";
import { ratchet } from "./ratchet.ts";

// axe-core on every tab and each of System's sections, with the robot off and on. Violations are counted per
// rule (one per offending element) against e2e/baselines/, so new ones fail
// and existing ones are tracked until they're fixed.

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

for (const power of ["off", "on"] as const) {
  test(`accessibility, robot ${power}`, async ({ page }, info) => {
    await open(page);
    await setPower(page, power === "on");
    const counts: Record<string, number> = {};
    const details: Record<string, unknown[]> = {};
    // An element a tab shows in each of its sections (its own panel) counts once.
    const seen = new Set<string>();
    for (const v of VIEWS) {
      const name = viewName(v);
      await view(page, v);
      // The app UIs are third-party pages; only the dashboard is ours.
      const { violations } = await new AxeBuilder({ page }).withTags(TAGS).exclude("iframe").analyze();
      for (const violation of violations) {
        const nodes = violation.nodes.filter((node) => {
          const key = `${v.tab} ${violation.id} ${node.target.join(" ")}`;
          return !seen.has(key) && Boolean(seen.add(key));
        });
        if (nodes.length === 0) continue;
        counts[violation.id] = (counts[violation.id] ?? 0) + nodes.length;
        (details[violation.id] ??= []).push(
          ...nodes.map((node) => ({ tab: name, impact: violation.impact, help: violation.help, target: node.target.join(" "), html: node.html.slice(0, 200) })),
        );
      }
    }
    await setPower(page, false);
    ratchet(info, `a11y-${power}`, counts, details);
  });
}
