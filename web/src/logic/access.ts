// System › Access (#334): devices and profiles as one list, and the browsers
// waiting for a code to be approved.

import type { ControlHolder } from "../api/control.ts";
import type { PairedDevice, ProfileItem } from "./auth.ts";

/** One item of GET /api/auth/pending: a browser waiting, never its code. */
export interface WaitingDevice {
  peer: string;
  /** Seconds since it asked. */
  age: number;
}

export function isWaitingList(value: unknown): value is { pending: WaitingDevice[] } {
  const list = (value as { pending?: unknown } | null)?.pending;
  return Array.isArray(list) && list.every((w) => typeof w?.peer === "string" && typeof w?.age === "number");
}

/** "asking now", "asking for 40 s", "asking for 3 min". */
export function waitingText(age: number): string {
  if (age < 10) return "asking now";
  if (age < 60) return `asking for ${Math.floor(age / 10) * 10} s`;
  return `asking for ${Math.floor(age / 60)} min`;
}

export interface AccessGroup {
  profile: ProfileItem;
  devices: PairedDevice[];
}

export interface AccessList {
  /** Only one person, an admin, and no shared device: a plain device list. */
  solo: boolean;
  people: AccessGroup[];
  /** Shared tablets: they belong to nobody. */
  shared: PairedDevice[];
}

/** Each profile with its own devices, then the shared ones. */
export function accessList(profiles: ProfileItem[], devices: PairedDevice[]): AccessList {
  const shared = devices.filter((d) => d.shared);
  const people = profiles.map((profile) => ({
    profile,
    devices: devices.filter((d) => !d.shared && profile.devices?.includes(d.id)),
  }));
  const solo = shared.length === 0 && people.length <= 1 && people.every((p) => p.profile.admin);
  return { solo, people, shared };
}

/**
 * The last admin device in `devices`: among all of them, the controller won't
 * revoke or demote it from a dashboard; among one person's own, demoting it
 * ends that person being an admin.
 */
export function isLastAdmin(devices: PairedDevice[], device: PairedDevice): boolean {
  return device.role === "admin" && devices.filter((d) => d.role === "admin").length === 1;
}

const ADMIN_CAN = "It can then open System, approve devices and install apps.";

/** What the waiting card's Admin button ("Approve as Admin") asks before it acts. `name`: what the admin typed for the device, if anything. */
export function approveAdminQuestion(name: string): string {
  return `Approve ${name || "this device"} as an admin? ${ADMIN_CAN}`;
}

/**
 * The warning for Revoke, Make admin and Demote while someone drives: each
 * reconnects the device and every device acting as its person (`auth.rs`
 * set_role and revoke), which ends a drive session there, and the robot
 * goes home. Control names its holder by device name and person, not id,
 * so a same-named device may warn too; it never misses one.
 */
export function motionWarning(holder: ControlHolder | null | undefined, device: PairedDevice): string {
  if (!holder) return "";
  const person = device.person || device.name;
  // Another device of the person: only a member's drive ends (its credential renews); an admin's goes on.
  const affected = holder.device === device.name || (!device.shared && !holder.admin && holder.person === person);
  return affected ? ` ${holder.device || holder.person} is in control: the robot may move back home.` : "";
}

/** What Make admin asks before it acts. `limited`: the person's profile restricts them today. `motion`: from motionWarning. */
export function makeAdminQuestion(device: PairedDevice, person: string, limited: boolean, motion = ""): string {
  const lifts = limited ? ` ${person}'s profile stops limiting every device of ${person}'s.` : "";
  return `Make ${device.name} an admin? ${ADMIN_CAN}${lifts}${motion}`;
}

/**
 * What Demote asks before it acts. `only`: the person's only admin device, so
 * their profile applies again. `motion`: from motionWarning. Its own browser
 * can't undo it.
 */
export function demoteQuestion(device: PairedDevice, person: string, only: boolean, motion = ""): string {
  const which = device.current ? `this browser (${device.name})` : device.name;
  const profile = only ? ` ${person}'s profile applies to every device of ${person}'s again.` : "";
  const own = device.current ? " Only another admin can make it an admin again." : "";
  return `Demote ${which}? It can no longer open System, approve devices or install apps.${profile}${own}${motion}`;
}
