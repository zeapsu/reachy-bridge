// The dashboard drives the app-loading tab through its URL hash:
// #waking, #starting, #opening, or #failed:<message>.

export const LOADING_STEPS = ["waking", "starting", "opening"] as const;
export type LoadingStep = (typeof LOADING_STEPS)[number];
export const LOADING_PROGRESS: Record<LoadingStep, number> = { waking: 22, starting: 55, opening: 88 };

export type LoadingState = { kind: "step"; index: number } | { kind: "failed"; message: string };

const DEFAULT_FAILURE = "The app didn't start.";

/** Parse `location.hash` (with or without the leading #). */
export function parseLoadingHash(rawHash: string): LoadingState {
  const raw = rawHash.startsWith("#") ? rawHash.slice(1) : rawHash;
  let hash: string;
  try {
    hash = decodeURIComponent(raw);
  } catch {
    hash = raw; // malformed escapes: show what we got rather than throw
  }
  hash = hash || "waking";
  if (hash.startsWith("failed")) {
    return { kind: "failed", message: hash.slice("failed:".length) || DEFAULT_FAILURE };
  }
  return { kind: "step", index: Math.max(0, LOADING_STEPS.indexOf(hash as LoadingStep)) };
}

/** The hash the dashboard writes for a step (`failed:<msg>` included). */
export function loadingHash(step: string): string {
  return encodeURIComponent(step);
}

/** URL the dashboard opens synchronously on click (popup blockers). */
export function loadingPageUrl(pagePath: string, appDisplay: string): string {
  return `${pagePath}?app=${encodeURIComponent(appDisplay)}#waking`;
}
