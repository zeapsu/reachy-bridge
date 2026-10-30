// Listen and talk (#219). Pure: the hook and panel use these.

import type { VoiceSession, VoiceView } from "../api/voice.ts";
import type { ControlState } from "./control.ts";

export const VOICE_POLL_MS = 2000;
/** How long a disconnected session gets to come back before this browser ends it. */
export const DISCONNECT_GRACE_MS = 8000;
/** How long the browser gathers its ICE candidates before answering. */
export const ICE_WAIT_MS = 3000;

/** Why this browser can't start listening or talking now, or null. */
export function voiceBlocked(view: VoiceView | null, control: ControlState, secure = true): string | null {
  // Audio only connects over the tailnet, where the dashboard is served over https;
  // a plain-http LAN page would open the robot's mic and never connect.
  if (!secure) return "Listening and talking work from the dashboard's https (Tailscale) address.";
  if (!view) return "Checking the robot's audio…";
  if (!view.available) return "Listening and talking work with the real robot, not the simulation.";
  if (control.kind === "other") return `${control.person} is in control of the robot.`;
  // Free with control to take: starting takes it (api/client.ts).
  if (control.kind === "free" && !control.canTake) return "Log in from a paired browser to listen or talk.";
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

/** The open session is this browser's: its login's, or the one it opened (`mine`, its ref). */
export const isMine = (s: VoiceSession, mine: string | null) => s.you || (mine !== null && s.ref === mine);

/** The mic-live badge every dashboard shows, or null with no session. */
export function voiceBadge(view: VoiceView | null, mine: string | null): string | null {
  const s = view?.session;
  if (!s) return null;
  return isMine(s, mine) ? `You're ${doing(s)}` : `${s.person || "Someone"} is ${doing(s)}`;
}
