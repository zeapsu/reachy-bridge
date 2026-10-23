export const MOVE_DATASETS = [
  { id: "expr-emotions", label: "Emotions", dataset: "pollen-robotics/reachy-mini-emotions-library" },
  { id: "expr-dances", label: "Dances", dataset: "pollen-robotics/reachy-mini-dances-library" },
] as const;

export const EXPRESSION_POLL_MS = 750;

export const HINT_OFFLINE = "Expressions enable when the daemon is online.";
export const HINT_APP_ACTIVE = "Expressions pause while an app is running.";
export const HINT_RETURNING_HOME = "Returning home…";

/** Move names from a dataset list response (array, or {moves|names|items}). */
export function parseMoveList(data: unknown): string[] {
  const list = Array.isArray(data)
    ? data
    : data && typeof data === "object"
      ? ((data as Record<string, unknown>).moves ??
        (data as Record<string, unknown>).names ??
        (data as Record<string, unknown>).items)
      : null;
  return Array.isArray(list) ? list.map((move) => String(move ?? "")) : [];
}

export interface ChipGates {
  daemonOnline: boolean;
  /** A discrete control request (motor, test sound, power...) is in flight. */
  controlBusy: boolean;
  /** An app owns the robot (#66). */
  appActive: boolean;
  /** An expression or return-home session is active. */
  exprBusy: boolean;
}

/** Expression chips only enable on an idle, online robot that no app owns. */
export function chipsDisabled(g: ChipGates): boolean {
  return g.exprBusy || !g.daemonOnline || g.controlBusy || g.appActive;
}

/** Why the expression session is busy; drives the hint. */
export type ExprBusyReason = "play" | "sync" | "returning" | null;

export interface HintInput {
  daemonOnline: boolean;
  loaded: boolean;
  appActive: boolean;
  busy: ExprBusyReason;
  /** Last play failure, cleared by the next play / reconcile / state change. */
  error: string | null;
}

export interface Hint {
  text: string;
  hidden: boolean;
  error: boolean;
}

/** The expressions panel hint for the current state. */
export function expressionHint(h: HintInput): Hint {
  if (!h.daemonOnline || !h.loaded) return { text: HINT_OFFLINE, hidden: false, error: false };
  if (h.error) return { text: h.error, hidden: false, error: true };
  if (h.busy === "returning") return { text: HINT_RETURNING_HOME, hidden: false, error: false };
  if (h.busy) return { text: "", hidden: true, error: false };
  if (h.appActive) return { text: HINT_APP_ACTIVE, hidden: false, error: false };
  return { text: "", hidden: true, error: false };
}
