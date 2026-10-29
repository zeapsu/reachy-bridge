// Listen and talk (#219): one WebRTC audio session with the robot.

export const voicePaths = {
  voice: "/api/audio",
  voiceSession: "/api/audio/session",
  voiceEnd: (id: string) => `/api/audio/session/${encodeURIComponent(id)}`,
  voiceAnswer: (id: string) => `/api/audio/session/${encodeURIComponent(id)}/answer`,
  voiceRoute: (id: string) => `/api/audio/session/${encodeURIComponent(id)}/route`,
} as const;

export interface VoiceRoute {
  /** Hear the robot's mic here. */
  listen: boolean;
  /** Play this browser's mic on the robot's speaker. */
  talk: boolean;
}

/** The open session, as every dashboard sees it. */
export interface VoiceSession extends VoiceRoute {
  /** Only the owner's browser gets the id (every browser, without logins). */
  id: string | null;
  person: string | null;
  /** Unix seconds. */
  since: number;
  /** This browser's login owns it (false without logins: compare the id). */
  you: boolean;
}

/** GET /api/audio */
export interface VoiceView {
  /** False in sim mode: the sim has no mic or speaker. */
  available: boolean;
  session: VoiceSession | null;
}

/** POST /api/audio/session */
export interface VoiceOffer {
  id: string;
  offer: string;
}
