// Pure helpers for control (#221, #245): the control bar's text.

import type { ControlView } from "../api/control.ts";

/** How often the dashboard polls control; also the holder's presence signal. */
export const CONTROL_POLL_MS = 2000;

export type ControlState =
  | { kind: "off" }
  | { kind: "mine" }
  | {
      kind: "other";
      person: string;
      canTakeOver: boolean;
      /** The holder is this person on another device: its name (maybe ""). A robot action here moves control over. */
      elsewhere?: string;
    }
  | { kind: "free"; canTake: boolean };

/** What the control bar shows for this browser. */
export function controlState(view: ControlView | null): ControlState {
  if (!view || !view.admin_mode || !view.you) return { kind: "off" };
  const holder = view.holder ?? null;
  if (holder?.you) return { kind: "mine" };
  if (holder) {
    const canTakeOver = view.you.admin || view.you.can_take;
    const elsewhere = holder.person === view.you.person && view.you.can_take ? { elsewhere: holder.device } : {};
    return { kind: "other", person: holder.person, canTakeOver, ...elsewhere };
  }
  return { kind: "free", canTake: view.you.can_take };
}

/** Who has control, for the status pill: "you", a person, or null. */
export function holderLabel(state: ControlState): string | null {
  if (state.kind === "mine") return "you";
  if (state.kind === "other") return state.person;
  return null;
}

/**
 * Whether a view that acts on the robot shows the take strip. Not while
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
      return prev.kind !== "mine" ? "You're in control" : null;
    case "other":
      if (prev.kind === "other" && prev.person === next.person) return null;
      if (next.elsewhere !== undefined) return `You took control on ${next.elsewhere || "another device"}`;
      return prev.kind === "mine" ? `${next.person} took over` : `${next.person} took control`;
    case "free":
      if (prev.kind === "mine") return "You're no longer in control";
      return prev.kind === "other" ? `${prev.person} let go of control` : null;
  }
}
