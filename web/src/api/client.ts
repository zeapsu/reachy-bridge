// The fetch wrapper every call goes through.

import { LOGIN_REQUIRED_EVENT } from "../logic/auth.ts";

export class ApiError extends Error {}

let believedTarget: string | null = null;

/**
 * The robot this page believes the controller drives ("robot" or "sim",
 * #149). Every request names it, and the controller refuses an action from a
 * page that missed a switch instead of carrying it out on the other robot.
 */
export function setBelievedTarget(target: string | null): void {
  believedTarget = target;
}

export const getBelievedTarget = (): string | null => believedTarget;

/** The controller's refusal while nobody is in control (control.rs not_holding). */
export const NEEDS_CONTROL = "take control of the robot first";

/** Fired when a robot action was refused because nobody is in control and none could be taken. */
export const CONTROL_NEEDED_EVENT = "reachy:control-needed";

let controlTaker: (() => Promise<boolean>) | null = null;

/** Set by the control hook: takes control when nobody has it and this login may. */
export function setControlTaker(taker: (() => Promise<boolean>) | null): void {
  controlTaker = taker;
}

/**
 * Take control for a robot action when nobody is in control (#221), so the
 * person doesn't have to tap Take control first. Only for something the
 * person just did: a request with no user gesture behind it (a cleanup, a
 * poll) never takes control. `gesture`: the caller knows it is one (a drive
 * pad press, which on touch screens isn't a user activation yet, or a step
 * after a permission prompt or confirm dialog, which can outlast it). The
 * take itself only succeeds while nobody is in control (`?if_free`).
 */
export async function takeControlIfFree(gesture = false): Promise<boolean> {
  const activation = typeof navigator === "undefined" ? undefined : navigator.userActivation;
  if (!controlTaker || !(gesture || !activation || activation.isActive)) return false;
  return controlTaker();
}

/**
 * Fetch JSON; non-2xx throws with the response's `detail` (or `HTTP <status>`),
 * matching main.js so error text shown in the panels is unchanged.
 */
export async function apiJson<T = unknown>(
  path: string,
  options: RequestInit = {},
  /** take: false for cleanups, which never take control. */
  flags: { take?: boolean; retried?: boolean } = {},
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...((options.headers as Record<string, string> | undefined) ?? {}),
  };
  if (believedTarget && !headers["X-Reachy-Target"]) {
    headers["X-Reachy-Target"] = believedTarget;
  }
  if (options.body !== undefined && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, { ...options, headers });
  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }
  if (response.status === 401 && typeof window !== "undefined") {
    window.dispatchEvent(new Event(LOGIN_REQUIRED_EVENT));
  }
  if (!response.ok) {
    const detail =
      payload && typeof payload === "object" && "detail" in payload
        ? (payload as { detail: unknown }).detail
        : `HTTP ${response.status}`;
    // Nobody is in control: take it and try once more.
    if (response.status === 403 && detail === NEEDS_CONTROL && flags.take !== false && !flags.retried) {
      if (await takeControlIfFree()) return apiJson<T>(path, options, { ...flags, retried: true });
      if (typeof window !== "undefined") window.dispatchEvent(new Event(CONTROL_NEEDED_EVENT));
    }
    throw new ApiError(String(detail));
  }
  return payload as T;
}

export const errorText = (error: unknown, fallback: string): string =>
  (error instanceof Error && error.message) || fallback;
