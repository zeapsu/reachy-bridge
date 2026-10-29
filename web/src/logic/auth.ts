// Pure helpers for the login (#141, #221): the login gate, pairing poll
// outcomes and the devices list.

export type Role = "admin" | "member";

/** GET /api/auth/status */
export interface AuthStatus {
  lan: boolean;
  /** LAN mode or admin mode; missing from controllers before #221. */
  login_required?: boolean;
  /** An admin has paired: every browser logs in, tailnet included. */
  admin_mode?: boolean;
  authenticated: boolean;
  device?: string | null;
  person?: string | null;
  role?: Role | null;
}

/** One item of GET /api/auth/devices */
export interface PairedDevice {
  id: string;
  name: string;
  /** Unix seconds. */
  created: number;
  last_seen: number;
  peer: string;
  /** Who uses it; missing from controllers before #221. */
  person?: string;
  role?: Role;
  /** This browser's own device. */
  current: boolean;
}

export type PairStatus = "pending" | "approved" | "expired" | "locked";

/** Fired by apiJson on any 401, so the gate can switch to the login screen. */
export const LOGIN_REQUIRED_EVENT = "rb-login-required";

/** How often the login screen polls its pairing request. */
export const PAIR_POLL_MS = 2000;

/** The login screen shows when a login is required and this browser isn't paired. */
export function needsLogin(status: AuthStatus | null | undefined): boolean {
  return Boolean(status && (status.login_required ?? status.lan) && !status.authenticated);
}

/**
 * What the devices panel offers this browser: nothing, admin setup (no admin
 * yet), managing every device (an admin, or anyone before admin mode in LAN
 * mode), or only its own device (a member).
 */
export type DevicesView = "hidden" | "setup" | "manage" | "own";

export function devicesView(status: AuthStatus | null | undefined): DevicesView {
  if (!status) return "hidden";
  if (status.admin_mode) return status.role === "admin" ? "manage" : status.device ? "own" : "hidden";
  // Before #221 the controller has no admin_mode: the #141 panel, LAN mode only.
  if (status.admin_mode === undefined) return status.lan ? "manage" : "hidden";
  return status.lan ? "manage" : "setup";
}

/**
 * Outcome of one GET /api/auth/pair/{id}. A 404 means the controller forgot
 * the request (expired, restarted, or this browser lost its pair cookie).
 */
export function pairOutcome(httpStatus: number, body: unknown): PairStatus | "error" {
  if (httpStatus === 404) return "expired";
  if (httpStatus < 200 || httpStatus >= 300) return "error";
  const status = body && typeof body === "object" ? (body as { status?: unknown }).status : undefined;
  return status === "pending" || status === "approved" || status === "expired" || status === "locked" ? status : "error";
}

/** "123456" → "123 456", easier to read and type. */
export function formatCode(code: string): string {
  return code.length === 6 ? `${code.slice(0, 3)} ${code.slice(3)}` : code;
}

/** A typed code without spaces or dashes, or null unless it is exactly 6 digits. */
export function normalizeCode(input: string): string | null {
  const code = input.replace(/[\s-]/g, "");
  return /^\d{6}$/.test(code) ? code : null;
}

/** Message for a pairing request that can't be approved any more. */
export function pairEndedText(status: "expired" | "locked"): string {
  return status === "locked" ? "Too many wrong codes were tried for this request." : "This code expired.";
}

/** "just now", "12 min ago", "3 h ago", "5 d ago" (last_seen is recorded every 10 min). */
export function seenText(unixSeconds: number, nowMs: number): string {
  const minutes = Math.floor((nowMs / 1000 - unixSeconds) / 60);
  if (minutes < 10) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.floor(hours / 24)} d ago`;
}
