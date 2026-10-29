// The setup wizard's steps and reading the device check (#135).

import type { CheckResult, SetupStatus } from "../api/setup.ts";

export const SETUP_STEPS = ["Device check", "Robot", "Admin", "Done"] as const;
export type SetupStep = (typeof SETUP_STEPS)[number];

/** Whether a status reply comes from the setup page (the dashboard has no such route). */
export function isSetupStatus(body: unknown): body is SetupStatus {
  return !!body && typeof body === "object" && (body as { setup?: unknown }).setup === true;
}

/** "12 pass · 1 warning · 1 failed", in that order, skipping zeros (skips aren't counted). */
export function checkSummary(results: CheckResult[]): string {
  const count = (status: CheckResult["status"]) => results.filter((r) => r.status === status).length;
  const parts: string[] = [];
  const pass = count("pass");
  const warn = count("warn");
  const fail = count("fail");
  if (pass) parts.push(`${pass} pass`);
  if (warn) parts.push(`${warn} ${warn === 1 ? "warning" : "warnings"}`);
  if (fail) parts.push(`${fail} failed`);
  return parts.join(" · ") || "nothing checked";
}

/** The chip's dot class for a check status. */
export function checkDot(status: CheckResult["status"]): string {
  return status === "pass" ? "ok" : status === "warn" ? "loading" : status === "fail" ? "error" : "";
}

/** "14:52 left", from seconds. */
export function timeLeft(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} left`;
}
