// Logs, board stats, the platform report and the robot state stream.

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
  /** Metres and radians about the head pivot. */
  head_pose?: number[] | { x?: number; y?: number; z?: number; roll?: number; pitch?: number; yaw?: number } | null;
  /** Body yaw then the six Stewart horn angles, radians. */
  head_joints?: number[] | null;
  body_yaw?: number | null;
  antennas_position?: number[] | { left?: number; right?: number } | null;
  doa?: { angle?: number; speech_detected?: boolean } | null;
  control_mode?: string | null;
}

export const systemPaths = {
  logSources: "/api/logs/sources",
  logStream: (source: string) => `/api/logs/stream?source=${encodeURIComponent(source)}`,
  statsStream: "/api/system/stats-stream",
  platform: "/api/system/platform",
  stateStream: "/telemetry/state-stream",
} as const;
