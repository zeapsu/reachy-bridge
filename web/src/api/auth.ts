// LAN login and device pairing (#141).

import { ApiError, apiJson } from "./client.ts";

export const authPaths = {
  authStatus: "/api/auth/status",
  authPair: "/api/auth/pair",
  authPairPoll: (id: string) => `/api/auth/pair/${encodeURIComponent(id)}`,
  authApprove: "/api/auth/approve",
  authDevices: "/api/auth/devices",
  authDevice: (id: string) => `/api/auth/devices/${encodeURIComponent(id)}`,
  // Admin approval from the dashboard (#334).
  authPending: "/api/auth/pending",
  authDeviceRole: (id: string) => `/api/auth/devices/${encodeURIComponent(id)}/role`,
  // Profiles (#297).
  authProfiles: "/api/auth/profiles",
  authProfile: (name: string) => `/api/auth/profiles/${encodeURIComponent(name)}`,
  authPick: "/api/auth/pick",
} as const;

/**
 * Sends a role change or revoke (#341). Either can end someone's drive, and the
 * robot then moves back home, so the controller refuses one that would (428,
 * "<device> is in control: the robot may move back home.") unless `?motion=ok`
 * says the admin was told. `warned`: the question already said so. Otherwise a
 * refusal asks again with the controller's warning, and a yes sends it told.
 * The controller checks as it makes the change, so control changing while a
 * question is open can't slip through. Resolves false on a no.
 */
export async function sendTold(path: string, init: RequestInit, warned: boolean, ask: (warning: string) => boolean): Promise<boolean> {
  const send = (told: boolean) => apiJson(told ? `${path}?motion=ok` : path, init);
  try {
    await send(warned);
    return true;
  } catch (err) {
    if (warned || !(err instanceof ApiError) || err.status !== 428) throw err;
    if (!ask(err.message)) return false;
    await send(true);
    return true;
  }
}
