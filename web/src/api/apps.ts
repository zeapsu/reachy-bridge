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
  /** The Hugging Face Space it was installed from, owner/name. */
  space?: string | null;
  author?: string | null;
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
  /** The controller's app UI proxy over plain http; null if it isn't listening. */
  proxy_port?: number | null;
}

/** GET/PUT /api/apps/favorites */
export interface Favorites {
  favorites: string[];
}

/** One entry of GET /api/apps/catalog: a Hugging Face Space (#126). */
export interface CatalogApp {
  /** The Space id, owner/name. */
  id: string;
  /** The installed package name. */
  name: string;
  title: string;
  description: string;
  likes: number;
  author: string;
  /** ISO time of the Space's last change. */
  updated: string | null;
  private: boolean;
  installed: boolean;
}

/** GET /api/apps/jobs/{id}: an install, update or removal. */
export interface AppJob {
  status: "pending" | "in_progress" | "done" | "failed" | string;
  logs: string[];
}

export const appsPaths = {
  appsStatus: "/api/apps/status",
  appsFavorites: "/api/apps/favorites",
  appStart: (name: string) => `/api/apps/start/${encodeURIComponent(name)}`,
  appStop: (name: string) => `/api/apps/stop/${encodeURIComponent(name)}`,
  appUi: (name: string) => `/api/apps/${encodeURIComponent(name)}/ui`,
  appCatalog: (all: boolean) => `/api/apps/catalog${all ? "?all=true" : ""}`,
  appInstall: "/api/apps/install",
  appInstallPrivate: "/api/apps/install-private",
  appJob: (id: string) => `/api/apps/jobs/${encodeURIComponent(id)}`,
  appRemove: (name: string) => `/api/apps/remove/${encodeURIComponent(name)}`,
  appUpdates: "/api/apps/updates",
  appUpdate: (name: string) => `/api/apps/update/${encodeURIComponent(name)}`,
} as const;
