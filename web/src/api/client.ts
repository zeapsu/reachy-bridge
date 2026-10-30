// The fetch wrapper every call goes through.

import { LOGIN_REQUIRED_EVENT } from "../logic/auth.ts";

export class ApiError extends Error {
  /** The HTTP status, when the error came from a response. */
  constructor(
    message: string,
    readonly status = 0,
  ) {
    super(message);
  }
}

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
/** Its refusal while someone holds control: "<person> is in control of the robot". */
const HELD = / is in control of the robot$/;

/**
 * Fired (a CustomEvent, the refusal as its detail) when a robot action was
 * refused for control and none could be taken: nobody, or someone else, is
 * in control.
 */
export const CONTROL_NEEDED_EVENT = "reachy:control-needed";

let controlTaker: (() => Promise<boolean>) | null = null;

/** Set by the control hook: takes control when nobody has it and this login may. */
export function setControlTaker(taker: (() => Promise<boolean>) | null): void {
  controlTaker = taker;
}

/**
 * Take control for a robot action when nobody is in control, or this
 * person holds it on another device (#221), so they don't have to tap Take
 * control first. Only for something the
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
    // Nobody is in control, or this person on another device: take it and try once more.
    const control = response.status === 403 && typeof detail === "string" && (detail === NEEDS_CONTROL || HELD.test(detail));
    if (control && flags.take !== false && !flags.retried) {
      if (await takeControlIfFree()) return apiJson<T>(path, options, { ...flags, retried: true });
      if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent(CONTROL_NEEDED_EVENT, { detail }));
    }
    throw new ApiError(String(detail), response.status);
  }
  return payload as T;
}

export const errorText = (error: unknown, fallback: string): string =>
  (error instanceof Error && error.message) || fallback;
