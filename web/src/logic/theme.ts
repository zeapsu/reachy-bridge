export const THEME_KEY = "reachy-theme";
export const THEME_SCHEMES = [
  { id: "amber", label: "Amber", sw: "#ffc857" },
  { id: "arctic", label: "Arctic", sw: "#7fe0ff" },
  { id: "emerald", label: "Emerald", sw: "#3ce3a0" },
  { id: "ultraviolet", label: "Violet", sw: "#c199ff" },
  { id: "ember", label: "Ember", sw: "#ff8a5c" },
] as const;

export function currentScheme(): string {
  return document.documentElement.dataset.scheme || "amber";
}

export function applyScheme(scheme: string): void {
  document.documentElement.dataset.scheme = scheme;
  try {
    localStorage.setItem(THEME_KEY, scheme);
  } catch {
    // storage blocked: the choice lasts for this page only
  }
}
