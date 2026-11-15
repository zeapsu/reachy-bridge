// Family mode (#297): what the dashboard shows, dims and hides for the
// acting profile. The controller enforces all of it; this mirrors it.

import type { Can, Kind, ProfileView } from "./auth.ts";

export const ALL_CAN: Can = { power: true, drive: true, voice: true, camera: true };

/** A new kid's switches: power and play, no microphone or camera. */
export const KID_CAN: Can = { power: true, drive: true, voice: false, camera: false };

/** What this browser may do: everything without a profile (no admin mode). */
export interface Rights {
  /** A profile applies (admin mode). */
  family: boolean;
  admin: boolean;
  /** System: the admin's only. */
  system: boolean;
  /** The Controls pane: not for kids. */
  controls: boolean;
  can: Can;
  apps: string[] | null;
  /** A shared device nobody has picked a profile on. */
  picking: boolean;
}

export function rights(profile: ProfileView | null | undefined): Rights {
  if (!profile) return { family: false, admin: true, system: true, controls: true, can: ALL_CAN, apps: null, picking: false };
  return {
    family: true,
    admin: profile.admin,
    system: profile.admin,
    controls: profile.admin || profile.kind !== "kid",
    can: profile.admin ? ALL_CAN : profile.can,
    apps: profile.admin ? null : profile.apps,
    picking: profile.shared && !profile.name,
  };
}

/** The installed apps a profile sees, in their order. */
export function allowedApps(names: string[], apps: string[] | null): string[] {
  return apps === null ? names : names.filter((name) => apps.includes(name));
}

/** A profile's avatar hue, from its name (stable across devices). */
export function profileHue(name: string): number {
  let hash = 7;
  for (const ch of name) hash = (hash * 31 + ch.codePointAt(0)!) >>> 0;
  return hash % 360;
}

export function profileInitial(name: string): string {
  return (/[\p{L}\p{N}]/u.exec(name)?.[0] ?? "?").toUpperCase();
}

export const KIND_LABEL: Record<Kind, string> = { grown: "Grown-up", kid: "Kid" };

/** "3 apps", "All apps", "No apps". */
export function appsText(apps: string[] | null | undefined): string {
  if (apps === null || apps === undefined) return "All apps";
  if (apps.length === 0) return "No apps";
  return `${apps.length} app${apps.length === 1 ? "" : "s"}`;
}
