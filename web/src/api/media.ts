// Audio, camera and the head wobble.

/** GET /api/volume/current and /api/volume/microphone/current */
export interface Volume {
  volume: number;
}

/** GET /api/camera/availability */
export interface CameraAvailability {
  available: boolean;
  reason?: "available" | "daemon_down" | "media_unavailable" | string;
  holder?: null;
}

export const mediaPaths = {
  volumeCurrent: "/api/volume/current",
  volumeSet: "/api/volume/set",
  micCurrent: "/api/volume/microphone/current",
  micSet: "/api/volume/microphone/set",
  testSound: "/api/volume/test-sound",
  wobble: (on: boolean) => `/api/media/wobbling/${on ? "enable" : "disable"}`,
  cameraAvailability: "/api/camera/availability",
  cameraStream: (ts: number) => `/api/camera/stream?ts=${ts}`,
  audioStream: "/api/audio/levels-stream",
} as const;
