export const MOVE_DATASETS = [
  { id: "expr-emotions", label: "Emotions", dataset: "pollen-robotics/reachy-mini-emotions-library" },
  { id: "expr-dances", label: "Dances", dataset: "pollen-robotics/reachy-mini-dances-library" },
] as const;

export const EXPRESSION_POLL_MS = 750;

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

/** Why the expression session is busy. */
export type ExprBusyReason = "play" | "sync" | "returning" | null;

/** A play failure worth showing: greyed tiles explain every other state. */
export function expressionError(daemonOnline: boolean, loaded: boolean, error: string | null): string | null {
  return daemonOnline && loaded ? error : null;
}
