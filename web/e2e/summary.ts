// Prints the browser tests' metrics (test-results/metrics/*.json) as
// Markdown for the CI job summary: each count next to its baseline, and the
// dashboard's load time.
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const dir = join("test-results", "metrics");
const files = existsSync(dir) ? readdirSync(dir).filter((f) => f.endsWith(".json")).sort() : [];
const lines = ["## Browser tests", ""];
if (files.length === 0) lines.push("No metrics were written.");
else {
  lines.push("| Check | Count | Baseline |", "|---|---:|---:|");
  for (const file of files) {
    const { counts, details } = JSON.parse(readFileSync(join(dir, file), "utf8"));
    const basePath = join("e2e", "baselines", file);
    const baseline = existsSync(basePath) ? JSON.parse(readFileSync(basePath, "utf8")) : {};
    const name = file.replace(/\.json$/, "");
    const keys = [...new Set([...Object.keys(counts), ...Object.keys(baseline)])].sort();
    if (keys.length === 0) lines.push(`| ${name} | 0 | 0 |`);
    for (const key of keys) lines.push(`| ${name}: ${key} | ${counts[key] ?? 0} | ${baseline[key] ?? 0} |`);
    if (typeof details?.loadMs === "number") lines.push(`| ${name}: load time (ms) | ${details.loadMs} | |`);
  }
}
console.log(lines.join("\n"));
