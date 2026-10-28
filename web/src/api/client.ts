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

/**
 * Fetch JSON; non-2xx throws with the response's `detail` (or `HTTP <status>`),
 * matching main.js so error text shown in the panels is unchanged.
 */
export async function apiJson<T = unknown>(path: string, options: RequestInit = {}): Promise<T> {
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
    throw new ApiError(String(detail));
  }
  return payload as T;
}

export const errorText = (error: unknown, fallback: string): string =>
  (error instanceof Error && error.message) || fallback;
