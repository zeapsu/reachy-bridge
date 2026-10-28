import type { CatalogApp } from "../api.ts";

/** A Hugging Face Space id, owner/name (matches the controller's check). */
export function validSpaceId(id: string): boolean {
  const parts = id.trim().split("/");
  return parts.length === 2 && parts.every((p) => p.length > 0 && p.length <= 96 && /^[A-Za-z0-9._-]+$/.test(p) && p !== "." && p !== "..");
}

/** The Space's page on Hugging Face, where its README says what it does. */
export function spaceUrl(id: string | null | undefined): string | null {
  return id && validSpaceId(id) ? `https://huggingface.co/spaces/${id.trim()}` : null;
}

/** "by owner · 12 likes · updated Jun 2026", from what's known. */
export function appMeta(details: { author?: string | null; likes?: number; updated?: string | null }): string {
  const parts: string[] = [];
  if (details.author) parts.push(`by ${details.author}`);
  if (details.likes) parts.push(`${details.likes} like${details.likes === 1 ? "" : "s"}`);
  const updated = details.updated ? new Date(details.updated) : null;
  if (updated && !Number.isNaN(updated.getTime())) {
    parts.push(`updated ${updated.toLocaleDateString("en", { month: "short", year: "numeric", timeZone: "UTC" })}`);
  }
  return parts.join(" · ");
}

/** Catalog entries matching a search, by title, description or Space id. */
export function filterCatalog(apps: CatalogApp[], query: string): CatalogApp[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return apps;
  return apps.filter((app) => {
    const text = `${app.title} ${app.description} ${app.id}`.toLowerCase();
    return words.every((w) => text.includes(w));
  });
}

/** The last non-empty log line, for a row's progress text. */
export function jobLine(logs: string[]): string {
  for (let i = logs.length - 1; i >= 0; i--) {
    const line = logs[i].trim();
    if (line) return line.length > 90 ? `${line.slice(0, 87)}…` : line;
  }
  return "";
}

export function jobFinished(status: string): boolean {
  return status === "done" || status === "failed";
}

/** A Hugging Face access token's shape (the controller checks it too). */
export function tokenShape(token: string): boolean {
  const t = token.trim();
  return t.length > 0 && t.length <= 256 && /^[A-Za-z0-9_-]+$/.test(t);
}
