import type { AppStatus } from "../api.ts";

export const MAX_FAVORITES = 3;
export const LOADING_FRAGMENTS = [
  "Booting…",
  "Resolving env…",
  "Connecting to daemon…",
  "Activating motors…",
  "Waking up…",
];

/** The app that owns the robot: running, loading or stopping. */
export function activeApp(statuses: AppStatus[]): AppStatus | null {
  return (
    statuses.find((app) => app.status === "running" || app.status === "loading" || app.status === "stopping") ??
    null
  );
}

export function appForName(statuses: AppStatus[], name: string): AppStatus {
  return statuses.find((item) => item.name === name) ?? { name, status: "stopped" };
}

/**
 * Keep at most three unique string favorites, dropping any that are no longer
 * installed (once the installed list is known, i.e. non-empty).
 */
export function normalizeFavorites(values: unknown, installed: string[]): string[] {
  if (!Array.isArray(values)) return [];
  const seen = new Set<string>();
  return values.filter((name): name is string => {
    if (typeof name !== "string" || seen.has(name) || seen.size >= MAX_FAVORITES) return false;
    if (installed.length && !installed.includes(name)) return false;
    seen.add(name);
    return true;
  });
}

/**
 * Names shown in the favorites panel: favorites, the active app, and every
 * errored non-favorite app (so a failure is never hidden), in installed order.
 */
export function visibleAppNames(installed: string[], statuses: AppStatus[], favorites: string[]): string[] {
  const active = activeApp(statuses);
  const failed = new Set(
    statuses.filter((app) => app.status === "error" && !favorites.includes(app.name)).map((app) => app.name),
  );
  return installed.filter((name) => favorites.includes(name) || active?.name === name || failed.has(name));
}

export type CardKind = "running" | "loading" | "stopping" | "error" | "";

export interface CardStatus {
  kind: CardKind;
  /** Class on .app-card. */
  statusClass: CardKind;
  /** Class on the card's .dot. */
  dotClass: "live" | "loading" | "stopping" | "error" | "";
}

export function cardStatus(app: AppStatus): CardStatus {
  switch (app.status) {
    case "running":
      return { kind: "running", statusClass: "running", dotClass: "live" };
    case "loading":
      return { kind: "loading", statusClass: "loading", dotClass: "loading" };
    case "stopping":
      return { kind: "stopping", statusClass: "stopping", dotClass: "stopping" };
    case "error":
      return { kind: "error", statusClass: "error", dotClass: "error" };
    default:
      return { kind: "", statusClass: "", dotClass: "" };
  }
}

export type AppAction =
  | { kind: "running"; showOpen: boolean; stopDisabled: boolean }
  | { kind: "pending"; label: "Loading" | "Stopping" }
  | { kind: "start"; disabled: boolean; title: string };

export interface ActionGates {
  /** Some app (maybe this one) owns the robot. */
  anyActive: boolean;
  /** An app start/stop request is in flight. */
  appBusy: boolean;
  daemonOnline: boolean;
  /** An expression (or a return-home) is playing. */
  exprBusy: boolean;
}

/** The action button for a card, with every start guard main.js applies. */
export function appAction(app: AppStatus, gates: ActionGates): AppAction {
  const { kind } = cardStatus(app);
  if (kind === "running") return { kind: "running", showOpen: Boolean(app.has_ui), stopDisabled: gates.appBusy };
  if (kind === "loading") return { kind: "pending", label: "Loading" };
  if (kind === "stopping") return { kind: "pending", label: "Stopping" };
  const offline = !gates.daemonOnline;
  const title = offline
    ? "Apps start when the daemon is online."
    : gates.exprBusy
      ? "Apps start when the robot finishes its expression."
      : "";
  return { kind: "start", disabled: gates.anyActive || gates.appBusy || offline || gates.exprBusy, title };
}

/** Next favorites after toggling `name`, or an error when over the limit. */
export function toggleFavoriteList(
  current: string[],
  name: string,
): { ok: true; next: string[] } | { ok: false; error: string } {
  const next = current.includes(name) ? current.filter((item) => item !== name) : [...current, name];
  if (next.length > MAX_FAVORITES) return { ok: false, error: "Choose up to 3 favorite apps." };
  return { ok: true, next };
}

export function formatUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  const total = Math.floor(seconds);
  const minutes = Math.floor(total / 60);
  const hours = Math.floor(minutes / 60);
  if (hours > 0) return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
  return `${minutes}m ${String(total % 60).padStart(2, "0")}s`;
}

/** " · 3m 07s" for a running app with a start time, else "". */
export function uptimeSuffix(app: AppStatus, nowMs: number): string {
  if (app.status !== "running" || !app.start_time) return "";
  return ` · ${formatUptime(nowMs / 1000 - Number(app.start_time))}`;
}

/** Message under the panel after a start/stop request succeeded. */
export function actionResultMessage(name: string, status: string | undefined): string {
  return status ? `${name} ${status}` : "";
}

/**
 * HTTPS port of the controller's app UI proxy (`reachy-bridge serve`): it
 * forwards to whichever app is running.
 */
export const APP_UI_HTTPS_PORT = 8043;

/**
 * The same proxy over plain http when the controller doesn't say
 * (`CONTROLLER_APP_PORT`, default 7863): on loopback, or on the LAN in LAN
 * mode behind the dashboard's login. Apps themselves listen on loopback only
 * (#186), so a browser can't open an app's own port.
 */
export const APP_UI_HTTP_PORT = 7863;

/**
 * The running app's UI through the controller's proxy, on the robot host the
 * dashboard was loaded from. Over HTTPS the app's page is a secure context
 * too (microphone, camera).
 */
export function appUiUrl(
  hostname: string,
  proxyPort: number = APP_UI_HTTP_PORT,
  protocol: string = globalThis.location?.protocol ?? "http:",
): string {
  if (protocol === "https:") return `https://${hostname}:${APP_UI_HTTPS_PORT}/`;
  return `http://${hostname}:${proxyPort}/`;
}

export const APP_UI_WAIT_ATTEMPTS = 60;
export const APP_UI_WAIT_INTERVAL_MS = 500;

/** A stable hue (0..359) for an app's monogram tile, from its name. */
export function appHue(name: string): number {
  let hash = 0;
  for (const ch of name) hash = (hash * 31 + ch.codePointAt(0)!) >>> 0;
  return hash % 360;
}

/** The monogram on an app's tile: its display name's first letter or digit. */
export function appInitial(display: string): string {
  return (/[\p{L}\p{N}]/u.exec(display)?.[0] ?? "?").toUpperCase();
}
