// Pure helpers for control time (#221): the control bar's text and timing.

import type { ControlView } from "../api/control.ts";

/** How often the dashboard polls control; also the holder's presence signal. */
export const CONTROL_POLL_MS = 2000;
/** The holder is warned this long before its grant ends. */
export const WARN_BEFORE_S = 60;

/** Slots offered when asking or granting; null is no end. */
export const SLOTS: { label: string; minutes: number | null }[] = [
  { label: "15 min", minutes: 15 },
  { label: "30 min", minutes: 30 },
  { label: "1 hour", minutes: 60 },
  { label: "2 hours", minutes: 120 },
  { label: "no end", minutes: null },
];

/** "14:05" in the viewer's time zone. */
export function clock(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function slotText(minutes: number | null): string {
  if (minutes === null) return "no end";
  if (minutes < 60) return `${minutes} min`;
  const hours = minutes / 60;
  return Number.isInteger(hours) ? `${hours} h` : `${minutes} min`;
}

/** Where this person stands on asking the admin for control time. */
export type Asking = { kind: "none" } | { kind: "can_ask" } | { kind: "asked" } | { kind: "cooldown"; until: number };

export type ControlState =
  | { kind: "off" }
  | { kind: "mine"; until: number | null; endingSoon: boolean }
  | { kind: "other"; person: string; until: number | null; canTakeOver: boolean; asking: Asking }
  | { kind: "free"; canTake: boolean; asking: Asking };

/** What the control bar shows for this browser. */
export function controlState(view: ControlView | null): ControlState {
  if (!view || !view.admin_mode || !view.you) return { kind: "off" };
  const now = view.now ?? 0;
  const holder = view.holder ?? null;
  if (holder?.you) {
    return { kind: "mine", until: holder.until, endingSoon: holder.until !== null && holder.until - now <= WARN_BEFORE_S };
  }
  // Anyone without a grant (the admin needs none) can ask, while someone
  // else drives too.
  const person = view.you.person;
  let asking: Asking = { kind: "can_ask" };
  if (view.you.admin || view.you.grant) asking = { kind: "none" };
  else if (person && (view.requests ?? []).some((r) => r.person === person)) asking = { kind: "asked" };
  else if (view.you.cooldown_until) asking = { kind: "cooldown", until: view.you.cooldown_until };
  if (holder) {
    return { kind: "other", person: holder.person, until: holder.until, canTakeOver: view.you.admin || view.you.can_take, asking };
  }
  return { kind: "free", canTake: view.you.can_take, asking };
}
