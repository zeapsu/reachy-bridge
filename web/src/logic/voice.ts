// Listen and talk (#219). Pure: the hook and panel use these.

import type { VoiceSession, VoiceView } from "../api/voice.ts";
import type { ControlState } from "./control.ts";

export const VOICE_POLL_MS = 2000;
/** How long the browser gathers its ICE candidates before answering. */
export const ICE_WAIT_MS = 3000;

/** Why this browser can't start listening or talking now, or null. */
export function voiceBlocked(view: VoiceView | null, control: ControlState): string | null {
  if (!view) return "Checking the robot's audio…";
  if (!view.available) return "Listening and talking work with the real robot, not the simulation.";
  if (control.kind === "other" || control.kind === "free") return "Take control of the robot to listen or talk.";
  return null;
}

/** Why this browser can't talk at all, or null. */
export function talkBlocked(secure: boolean, hasMic: boolean): string | null {
  if (!secure) return "Talking needs the dashboard's https address: browsers only share a mic over https.";
  if (!hasMic) return "This browser can't share a microphone.";
  return null;
}

const doing = (s: VoiceSession) =>
  s.listen && s.talk ? "listening and talking" : s.talk ? "talking" : s.listen ? "listening" : "connected, muted";

/** The mic-live badge every dashboard shows, or null with no session. */
export function voiceBadge(view: VoiceView | null): string | null {
  const s = view?.session;
  if (!s) return null;
  return s.you ? `You're ${doing(s)}` : `${s.person || "Someone"} is ${doing(s)}`;
}
