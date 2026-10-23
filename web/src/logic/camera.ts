import type { CameraAvailability } from "../api.ts";

export type ViewMode = "camera" | "sim" | "schematic";

export const MODE_BADGE: Record<ViewMode, string> = { camera: "CAMERA", sim: "SIM · MUJOCO", schematic: "SCHEMATIC" };
export const MODE_META: Record<ViewMode, string> = {
  camera: "live camera · waiting",
  sim: "decorative preview · not live",
  schematic: "telemetry 10 hz",
};
export const modeName = (mode: ViewMode) => (mode === "sim" ? "simulation" : mode);

/** Placeholder text when the camera availability check says no. */
export function unavailableMessage(availability: CameraAvailability | null | undefined): string {
  const reason = availability?.reason;
  if (reason === "daemon_down") return "Daemon offline";
  if (reason === "media_unavailable") return "Camera media offline";
  return "Camera unavailable";
}
