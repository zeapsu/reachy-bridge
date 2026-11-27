// System › Access (#334): devices and profiles as one list, and the browsers
// waiting for a code to be approved.

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

/** The controller won't revoke or demote the last admin device from a dashboard. */
export function isLastAdmin(devices: PairedDevice[], device: PairedDevice): boolean {
  return device.role === "admin" && devices.filter((d) => d.role === "admin").length === 1;
}

const ADMIN_CAN = "It can then open System, approve devices and install apps.";

/** What Approve as Admin asks before it acts. `name`: what the admin typed for the device, if anything. */
export function approveAdminQuestion(name: string): string {
  return `Approve ${name || "this device"} as an admin? ${ADMIN_CAN}`;
}

/** What Make admin asks before it acts. `limited`: the person's profile restricts them today. */
export function makeAdminQuestion(device: PairedDevice, person: string, limited: boolean): string {
  const lifts = limited ? ` ${person}'s profile stops limiting every device of ${person}'s.` : "";
  return `Make ${device.name} an admin? ${ADMIN_CAN}${lifts}`;
}
