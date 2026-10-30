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
  | {
      kind: "other";
      person: string;
      until: number | null;
      canTakeOver: boolean;
      asking: Asking;
      /** The holder is this person on another device: its name. A robot action here moves control over. */
      elsewhere?: string;
    }
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
    const canTakeOver = view.you.admin || view.you.can_take;
    const elsewhere = holder.person === person && view.you.can_take ? { elsewhere: holder.device } : {};
    return { kind: "other", person: holder.person, until: holder.until, canTakeOver, asking, ...elsewhere };
  }
  return { kind: "free", canTake: view.you.can_take, asking };
}

/** Who has control, for the status pill: "you", a person, or null. */
export function holderLabel(state: ControlState): string | null {
  if (state.kind === "mine") return "you";
  if (state.kind === "other") return state.person;
  return null;
}

/**
 * Whether a view that acts on the robot shows the take/ask strip. Not while
 * this browser is in control, nor when nobody is and it may take control:
 * its first robot action takes it then (api/client.ts).
 */
export const showStrip = (state: ControlState): boolean =>
  state.kind === "other" || (state.kind === "free" && !state.canTake);

/** A short notice when who drives changes; null when nothing worth one did (or on first load). */
export function controlNotice(prev: ControlState, next: ControlState): string | null {
  if (prev.kind === "off" || next.kind === "off") return null;
  switch (next.kind) {
    case "mine":
      if (prev.kind !== "mine") return next.until === null ? "You're in control" : `You're in control until ${clock(next.until)}`;
      return next.endingSoon && !prev.endingSoon ? "Your control time ends in under a minute" : null;
    case "other":
      if (prev.kind === "mine") return next.elsewhere ? `You took control on ${next.elsewhere}` : `${next.person} took over`;
      return prev.kind === "other" && prev.person === next.person ? null : `${next.person} took control`;
    case "free":
      if (prev.kind === "mine") return "You're no longer in control";
      return prev.kind === "other" ? `${prev.person} let go of control` : null;
  }
}
