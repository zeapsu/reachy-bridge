// Typed client for the controller API. Paths and shapes match the Python
// controller (apps/reachy_controller routes); the Rust controller serves the same.

/** GET /api/daemon/status */
export interface DaemonStatus {
  reachable: boolean;
  running: boolean;
  manageable: boolean;
  /** offline | running | asleep | starting | stopping | stopped | not_initialized | error | ... */
  status: string;
  motors?: string | null;
  error?: unknown;
}

export type AppRunStatus = "stopped" | "loading" | "running" | "stopping" | "error";

/** One item of GET /api/apps/status */
export interface AppStatus {
  name: string;
  display?: string;
  blurb?: string;
  status: AppRunStatus | string;
  error?: string | null;
  /** Unix seconds, only while running. */
  start_time?: number | null;
  has_ui?: boolean;
}

/** POST /api/apps/start/{name} and /api/apps/stop/{name} */
export interface AppActionResult {
  name?: string;
  status?: string;
  ui_port?: number | null;
  error?: string;
}

/** GET /api/apps/{name}/ui */
export interface AppUi {
  name: string;
  port: number | null;
  ready: boolean;
}

/** GET/PUT /api/apps/favorites */
export interface Favorites {
  favorites: string[];
}

/** GET/PUT /api/daemon/kinematics */
export interface Kinematics {
  engine: string;
  active: string | null;
  available: string[];
  restarted?: boolean;
}

/** GET /api/volume/current and /api/volume/microphone/current */
export interface Volume {
  volume: number;
}

/** GET /api/motors/status */
export interface MotorStatus {
  mode: string;
}

/** GET /api/camera/availability */
export interface CameraAvailability {
  available: boolean;
  reason?: "available" | "daemon_down" | "media_unavailable" | string;
  holder?: null;
}

/** GET /api/expressions/current */
export interface ExpressionCurrent {
  active: boolean;
  phase?: "playing" | "returning_home" | string;
  dataset?: string | null;
  move?: string | null;
  uuid?: string | null;
}

/** GET /api/logs/sources */
export interface LogSources {
  sources: { id: string; status?: string }[];
}

/** GET /api/system/platform: what the runtime detected at startup (#140). */
export interface PlatformInfo {
  name?: string;
  tier?: string;
  model?: string;
  features?: string[];
  skipped?: string[];
  reachy_mini?: string;
}

/** SSE `stats-update` on /api/system/stats-stream */
export interface SystemStats {
  cpu_percent?: number | null;
  gpu_percent?: number | null;
  cpu_temp_c?: number | null;
  gpu_temp_c?: number | null;
  memory_used_mb?: number | null;
  memory_total_mb?: number | null;
  alsa_card?: number | string | null;
  alsa_pcm_volume?: number | null;
  pipewire_sink_volume?: number | null;
}

/** SSE `state-update` on /telemetry/state-stream (daemon full state, with_doa). */
export interface RobotState {
  head_pose?: number[] | { roll?: number; pitch?: number; yaw?: number } | null;
  body_yaw?: number | null;
  antennas_position?: number[] | { left?: number; right?: number } | null;
  doa?: { angle?: number; speech_detected?: boolean } | null;
  control_mode?: string | null;
}

export class ApiError extends Error {}

/**
 * Fetch JSON; non-2xx throws with the response's `detail` (or `HTTP <status>`),
 * matching main.js so error text shown in the panels is unchanged.
 */
export async function apiJson<T = unknown>(path: string, options: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...((options.headers as Record<string, string> | undefined) ?? {}),
  };
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

export const paths = {
  daemonStatus: "/api/daemon/status",
  daemonStart: "/api/daemon/start",
  daemonStop: "/api/daemon/stop",
  kinematics: "/api/daemon/kinematics",
  appsStatus: "/api/apps/status",
  appsFavorites: "/api/apps/favorites",
  appStart: (name: string) => `/api/apps/start/${encodeURIComponent(name)}`,
  appStop: (name: string) => `/api/apps/stop/${encodeURIComponent(name)}`,
  appUi: (name: string) => `/api/apps/${encodeURIComponent(name)}/ui`,
  // The dataset id contains a slash and is sent unencoded, as main.js does.
  moveDatasetList: (dataset: string) => `/api/move/recorded-move-datasets/list/${dataset}`,
  expressionPlay: (dataset: string, move: string) =>
    `/api/expressions/play/${dataset}/${encodeURIComponent(move)}`,
  expressionStop: "/api/expressions/stop",
  expressionCurrent: "/api/expressions/current",
  volumeCurrent: "/api/volume/current",
  volumeSet: "/api/volume/set",
  micCurrent: "/api/volume/microphone/current",
  micSet: "/api/volume/microphone/set",
  testSound: "/api/volume/test-sound",
  motorStatus: "/api/motors/status",
  motorSetMode: (mode: string) => `/api/motors/set_mode/${encodeURIComponent(mode)}`,
  wobble: (on: boolean) => `/api/media/wobbling/${on ? "enable" : "disable"}`,
  cameraAvailability: "/api/camera/availability",
  cameraStream: (ts: number) => `/api/camera/stream?ts=${ts}`,
  logSources: "/api/logs/sources",
  logStream: (source: string) => `/api/logs/stream?source=${encodeURIComponent(source)}`,
  statsStream: "/api/system/stats-stream",
  platform: "/api/system/platform",
  audioStream: "/api/audio/levels-stream",
  stateStream: "/telemetry/state-stream",
} as const;
