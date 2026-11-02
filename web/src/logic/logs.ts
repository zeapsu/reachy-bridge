export const LOG_CAP = 500;
export const LOG_DEFAULT_SOURCE = "controller";

export interface LogLine {
  id: number;
  text: string;
}

export type LogStatus =
  | "idle"
  | "connecting"
  | "reconnecting"
  | "live"
  | "empty"
  | "stopped"
  | "offline"
  | "unavailable"
  | string;

/** Text shown in the empty log pane for a source status. */
export function emptyLogMessage(status: LogStatus | undefined): string {
  if (status === "offline") return "robot service down";
  if (status === "unavailable") return "output isn't captured yet";
  return "waiting for output";
}

/** Pill text and dot class for a log status. */
export function logStatusDisplay(status: LogStatus | undefined): { text: string; dot: string } {
  const text = status === "unavailable" ? "not captured" : status || "idle";
  const dot =
    status === "live"
      ? "ok"
      : status === "offline"
        ? "error"
        : status === "connecting" || status === "reconnecting"
          ? "loading"
          : "";
  return { text, dot };
}

/**
 * Append `incoming` (anything, coerced to strings) to `current`, keeping only
 * the newest `cap` lines. Ids keep counting from `nextId` so React keys stay
 * stable across trims.
 */
export function appendCapped(
  current: LogLine[],
  incoming: unknown,
  nextId: number,
  cap = LOG_CAP,
): { lines: LogLine[]; nextId: number } {
  if (!Array.isArray(incoming) || incoming.length === 0) return { lines: current, nextId };
  let id = nextId;
  const added = incoming.map((line) => ({ id: id++, text: String(line ?? "") }));
  const merged = current.concat(added);
  return { lines: merged.length > cap ? merged.slice(merged.length - cap) : merged, nextId: id };
}

/** Source ids from /api/logs/sources, ignoring malformed entries. */
export function parseLogSources(payload: unknown): string[] {
  const sources = (payload as { sources?: unknown } | null)?.sources;
  if (!Array.isArray(sources)) return [];
  return sources
    .filter((item): item is { id: string } => Boolean(item) && typeof (item as { id?: unknown }).id === "string")
    .map((item) => item.id);
}

/** The source to stream after a sources fetch: keep the selection if offered. */
export function pickLogSource(selected: string, offered: string[]): string {
  return offered.includes(selected) ? selected : (offered[0] ?? LOG_DEFAULT_SOURCE);
}
