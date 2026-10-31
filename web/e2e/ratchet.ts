import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { expect, type TestInfo } from "@playwright/test";

/**
 * Counts that may only go down: accessibility violations per axe rule, UX
 * findings per check. Each run writes its counts to test-results/metrics/
 * (CI shows them in the job summary) and fails if any count is above the
 * committed baseline in e2e/baselines/. After fixing some, lower the
 * baseline with UPDATE_BASELINES=1, which writes the current counts.
 */
export type Counts = Record<string, number>;

export function ratchet(info: TestInfo, name: string, counts: Counts, details: unknown): void {
  const file = `${info.project.name}-${name}.json`;
  const out = join("test-results", "metrics", file);
  mkdirSync(dirname(out), { recursive: true });
  writeFileSync(out, JSON.stringify({ counts, details }, null, 2) + "\n");
  const baselinePath = join("e2e", "baselines", file);
  if (process.env.UPDATE_BASELINES) {
    mkdirSync(dirname(baselinePath), { recursive: true });
    writeFileSync(baselinePath, JSON.stringify(sorted(counts), null, 2) + "\n");
    return;
  }
  const baseline: Counts = existsSync(baselinePath) ? JSON.parse(readFileSync(baselinePath, "utf8")) : {};
  const worse = Object.entries(counts)
    .filter(([key, n]) => n > (baseline[key] ?? 0))
    .map(([key, n]) => `${key}: ${n} (baseline ${baseline[key] ?? 0})`);
  expect(worse, `${name} got worse than e2e/baselines/${file}; see ${out}`).toEqual([]);
}

function sorted(counts: Counts): Counts {
  return Object.fromEntries(Object.entries(counts).filter(([, n]) => n > 0).sort(([a], [b]) => a.localeCompare(b)));
}
