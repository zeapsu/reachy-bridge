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
    for (const key of keys) {
      const [n, base] = [counts[key] ?? 0, baseline[key] ?? 0];
      const flag = n > base ? " ❌" : n < base ? " (lower the baseline)" : "";
      lines.push(`| ${name}: ${key} | ${n}${flag} | ${base} |`);
    }
    if (typeof details?.loadMs === "number") lines.push(`| ${name}: load time (ms) | ${details.loadMs} | |`);
  }
}

// The voice tests' audio traffic: each tone's level at each output, per route.
const traffic = join("test-results", "voice-traffic");
const runs = existsSync(traffic) ? readdirSync(traffic).filter((f) => f.endsWith(".json")).sort() : [];
if (runs.length) {
  const on = (route: Record<string, boolean>) => Object.keys(route).filter((k) => route[k]).join(" + ") || "all off";
  const cell = (tones: Record<string, number>) =>
    Object.entries(tones)
      .map(([tone, level]) => `${tone} ${level.toFixed(3)}`)
      .join(", ");
  lines.push("", "### Audio traffic", "", "Tone levels (0 to 1) at each output: mic = the robot's mic, robot = what it plays, browser = this browser's mic.");
  for (const file of runs) {
    const { title, steps } = JSON.parse(readFileSync(join(traffic, file), "utf8")) as {
      title: string;
      steps: { route: Record<string, boolean>; heard: Record<string, Record<string, number>> }[];
    };
    lines.push("", `**${title}**`, "", "| Switches on | This browser | Robot speaker | Apps' mic |", "|---|---|---|---|");
    for (const { route, heard } of steps) lines.push(`| ${on(route)} | ${cell(heard.browser)} | ${cell(heard.speaker)} | ${cell(heard.apps)} |`);
  }
}
console.log(lines.join("\n"));
