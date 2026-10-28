// Apps: status, start and stop, their UIs, and favorites.

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

export const appsPaths = {
  appsStatus: "/api/apps/status",
  appsFavorites: "/api/apps/favorites",
  appStart: (name: string) => `/api/apps/start/${encodeURIComponent(name)}`,
  appStop: (name: string) => `/api/apps/stop/${encodeURIComponent(name)}`,
  appUi: (name: string) => `/api/apps/${encodeURIComponent(name)}/ui`,
} as const;
