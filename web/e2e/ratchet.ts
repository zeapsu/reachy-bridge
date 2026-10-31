import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { expect, type TestInfo } from "@playwright/test";

/**
 * Counts that may only go down: accessibility violations per axe rule, UX
 * findings per check. Each run writes its counts to test-results/metrics/
 * (CI shows them in the job summary) and fails if any count is above the
 * committed baseline in e2e/baselines/. A count below it is flagged on the
 * test and in the summary rather than failed, since a status message that
 * comes and goes can lower a count for one run; lower the baseline once it
 * holds, so the fix can't quietly come undone. UPDATE_BASELINES=1 writes the
 * current counts. Record baselines where
 * Hugging Face is reachable, as in CI: its content changes the page.
 */
export type Counts = Record<string, number>;

export function ratchet(info: TestInfo, name: string, counts: Counts, details: unknown): void {
  const file = `${info.project.name}-${name}.json`;
  const out = join("test-results", "metrics", file);
  mkdirSync(dirname(out), { recursive: true });
  writeFileSync(out, JSON.stringify({ counts, details }, null, 2) + "\n");
  const baselinePath = join("e2e", "baselines", file);
  if (process.env.UPDATE_BASELINES === "1") {
    mkdirSync(dirname(baselinePath), { recursive: true });
    writeFileSync(baselinePath, JSON.stringify(sorted(counts), null, 2) + "\n");
    return;
  }
  const baseline: Counts = existsSync(baselinePath) ? JSON.parse(readFileSync(baselinePath, "utf8")) : {};
  const keys = [...new Set([...Object.keys(counts), ...Object.keys(baseline)])];
  const worse = keys.filter((key) => (counts[key] ?? 0) > (baseline[key] ?? 0));
  const better = keys.filter((key) => (counts[key] ?? 0) < (baseline[key] ?? 0));
  const show = (key: string) => `${key}: ${counts[key] ?? 0} (baseline ${baseline[key] ?? 0})`;
  if (better.length) {
    info.annotations.push({ type: "lower the baseline", description: `${file}: ${better.map(show).join(", ")}` });
  }
  expect.soft(worse.map(show), `${name} got worse than e2e/baselines/${file}; see ${out}`).toEqual([]);
}

function sorted(counts: Counts): Counts {
  return Object.fromEntries(Object.entries(counts).filter(([, n]) => n > 0).sort(([a], [b]) => a.localeCompare(b)));
}
