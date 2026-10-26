/**
 * Appearance preferences (System · Appearance): light or dark, the accent
 * colour, and the wallpaper. They are per browser, kept in localStorage, and
 * work with the daemon offline. index.html applies the saved mode and accent
 * before first paint.
 */

export const SCHEME_KEY = "reachy-theme";
export const MODE_KEY = "reachy-mode";
export const WALL_KEY = "reachy-wall";
export const WALL_PHOTO_KEY = "reachy-wall-photo";
export const DIM_KEY = "reachy-wall-dim";

export const SCHEMES = [
  { id: "amber", label: "Amber", swatch: "#ffb547" },
  { id: "arctic", label: "Arctic", swatch: "#6fd6ff" },
  { id: "emerald", label: "Emerald", swatch: "#3ce3a0" },
  { id: "ultraviolet", label: "Violet", swatch: "#c199ff" },
  { id: "ember", label: "Ember", swatch: "#ff8a5c" },
] as const;

export type Mode = "auto" | "light" | "dark";

/** Built-in wallpapers; "studio" is the theme's own gradient. */
export const WALLS = [
  { id: "studio", label: "Studio", css: null },
  {
    id: "dawn",
    label: "Dawn",
    css: "radial-gradient(70% 60% at 25% 20%, #f3b8a0, transparent 70%), radial-gradient(60% 60% at 85% 80%, #8f9fe0, transparent 70%), linear-gradient(160deg, #e9c9c0, #9aa6d8)",
  },
  {
    id: "fjord",
    label: "Fjord",
    css: "radial-gradient(80% 60% at 70% 15%, #6fa3b8, transparent 70%), linear-gradient(180deg, #1d3a4a, #0c1820 80%)",
  },
  { id: "paper", label: "Paper", css: "radial-gradient(90% 70% at 40% 30%, #fbf8f2, #e2ddd3 80%)" },
] as const;

export type WallId = (typeof WALLS)[number]["id"] | "photo";

export const DIM_DEFAULT = 25;
export const DIM_MAX = 70;
/** Longest side of a stored photo: big enough for a screen, small enough for localStorage. */
export const PHOTO_MAX_PX = 1920;

export function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** False when storage refused it (blocked, or full for a large photo). */
export function write(key: string, value: string | null): boolean {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

export function savedMode(): Mode {
  const mode = read(MODE_KEY);
  return mode === "light" || mode === "dark" ? mode : "auto";
}

export function applyMode(mode: Mode): void {
  if (mode === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = mode;
  write(MODE_KEY, mode === "auto" ? null : mode);
}

export function savedScheme(): string {
  return document.documentElement.dataset.scheme || "amber";
}

export function applyScheme(scheme: string): void {
  document.documentElement.dataset.scheme = scheme;
  write(SCHEME_KEY, scheme);
}

export function savedWall(): WallId {
  const id = read(WALL_KEY);
  if (id === "photo") return read(WALL_PHOTO_KEY) ? "photo" : "studio";
  return WALLS.some((w) => w.id === id) ? (id as WallId) : "studio";
}

/** Clamp a dim percentage to 0..DIM_MAX; anything unreadable is the default. */
export function clampDim(value: unknown): number {
  const n = Number(value);
  if (value === null || value === "" || !Number.isFinite(n)) return DIM_DEFAULT;
  return Math.round(Math.min(DIM_MAX, Math.max(0, n)));
}

export function savedDim(): number {
  return clampDim(read(DIM_KEY));
}

/** Scale (w, h) so the longest side is at most `max`. */
export function fitWithin(w: number, h: number, max: number): { w: number; h: number } {
  const scale = Math.min(1, max / Math.max(w, h));
  return { w: Math.round(w * scale), h: Math.round(h * scale) };
}
