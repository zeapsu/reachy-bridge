import type { CameraAvailability } from "../api.ts";

export type ViewMode = "camera" | "3d";

export const MODE_META: Record<ViewMode, string> = {
  camera: "live camera · waiting",
  "3d": "telemetry 10 hz",
};

/** Placeholder text when the camera availability check says no. */
export function unavailableMessage(availability: CameraAvailability | null | undefined): string {
  const reason = availability?.reason;
  if (reason === "daemon_down") return "Camera off";
  if (reason === "media_unavailable") return "Camera unavailable";
  return "Camera unavailable";
}
