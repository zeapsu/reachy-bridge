// The Hugging Face login that private Spaces need (#126). The token is
// write-only: the controller passes it to the daemon and never returns it.

/** GET /api/hf/status, and the answer to PUT/DELETE /api/hf/token */
export interface HfLogin {
  logged_in: boolean;
  username: string | null;
}

export const hfPaths = {
  hfStatus: "/api/hf/status",
  hfToken: "/api/hf/token",
} as const;
