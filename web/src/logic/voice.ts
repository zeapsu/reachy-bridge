// Listen and talk (#219). Pure: the hook and panel use these.

import type { VoiceSession, VoiceView } from "../api/voice.ts";
import type { ControlState } from "./control.ts";

export const VOICE_POLL_MS = 2000;
/** How long a disconnected session gets to come back before this browser ends it. */
export const DISCONNECT_GRACE_MS = 8000;
/** How long the browser gathers its ICE candidates before answering. */
export const ICE_WAIT_MS = 3000;

/**
 * Whether the page was opened on a tailnet address (100.64.0.0/10 or
 * Tailscale's fd7a:115c:a1e0::/48), the raw IP rather than the https name.
 */
export function onTailnet(hostname: string): boolean {
  const v4 = /^(\d{1,3})\.(\d{1,3})\.\d{1,3}\.\d{1,3}$/.exec(hostname);
  if (v4) return Number(v4[1]) === 100 && Number(v4[2]) >= 64 && Number(v4[2]) <= 127;
  return /^\[?fd7a:115c:a1e0:/i.test(hostname);
}

/**
 * Why this browser can't start listening or talking now, or null.
 * `tailnet`: the page is secure (the https name) or on a tailnet IP.
 */
export function voiceBlocked(view: VoiceView | null, control: ControlState, tailnet = true): string | null {
  // Audio only connects between tailnet addresses; a LAN page would open the
  // robot's mic and never connect. Listening needs no secure page, so a plain-http
  // page on the device's tailnet IP works too.
  if (!tailnet) return "Needs Tailscale address";
  if (!view) return "Checking audio…";
  if (!view.available) return "Not in simulation";
  if (control.kind === "other") return `${control.person} is in control of the robot.`;
  // Free with control to take: starting takes it (api/client.ts).
  if (control.kind === "free" && !control.canTake) return "Log in first";
  return null;
}

/** Why this browser can't talk at all, or null. */
export function talkBlocked(secure: boolean, hasMic: boolean): string | null {
  if (!secure) return "Needs https";
  if (!hasMic) return "No microphone here";
  return null;
}

function doing(s: VoiceSession): string {
  const parts = [
    s.listen && "listening",
    s.hear && "hearing what Reachy says",
    s.talk && "talking",
    s.apps && "talking to its apps",
  ].filter(Boolean);
  if (parts.length === 0) return "connected, muted";
  return parts.length === 1 ? String(parts[0]) : `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

/** The open session is this browser's: its login's, or the one it opened (`mine`, its ref). */
export const isMine = (s: VoiceSession, mine: string | null) => s.you || (mine !== null && s.ref === mine);

export type VoiceRoute = "listen" | "hear" | "talk" | "apps";

/** Who the badge names and which routes are live, or null with no session. */
export function voiceLive(view: VoiceView | null, mine: string | null): { who: string; routes: VoiceRoute[] } | null {
  const s = view?.session;
  if (!s) return null;
  const routes = (["listen", "hear", "talk", "apps"] as const).filter((route) => s[route]);
  return { who: isMine(s, mine) ? "You" : s.person || "Someone", routes };
}

/** The mic-live badge's full sentence (tooltip, screen readers), or null with no session. */
export function voiceBadge(view: VoiceView | null, mine: string | null): string | null {
  const s = view?.session;
  if (!s) return null;
  return isMine(s, mine) ? `You're ${doing(s)}` : `${s.person || "Someone"} is ${doing(s)}`;
}

/** Quieter than this (dBFS) shows an empty meter: room noise after noise suppression. */
export const METER_FLOOR_DB = -60;
/** This loud (dBFS) or louder fills the meter: speaking close to the mic. */
export const METER_TOP_DB = -12;
/** How much of the meter falls back per second once you stop talking. */
export const METER_FALL_PER_S = 1.5;

/** The RMS of one block of samples (-1..1). */
export function rms(samples: ArrayLike<number>): number {
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return samples.length ? Math.sqrt(sum / samples.length) : 0;
}

/** A level (RMS, 0..1) as a meter fill, 0..1, on a dB scale like a mixer's. */
export function meterFill(level: number): number {
  if (level <= 0) return 0;
  const db = 20 * Math.log10(level);
  return Math.min(1, Math.max(0, (db - METER_FLOOR_DB) / (METER_TOP_DB - METER_FLOOR_DB)));
}

/** The next shown fill: jumps up at once, falls back smoothly (`dt` seconds since the last frame). */
export function meterStep(shown: number, fill: number, dt: number): number {
  return fill >= shown ? fill : Math.max(fill, shown - METER_FALL_PER_S * dt);
}

/** How long a new mic may stay silent before the panel offers other inputs. */
export const MIC_SILENT_MS = 4000;
