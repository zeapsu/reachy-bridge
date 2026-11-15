// LAN login and device pairing (#141).

export const authPaths = {
  authStatus: "/api/auth/status",
  authPair: "/api/auth/pair",
  authPairPoll: (id: string) => `/api/auth/pair/${encodeURIComponent(id)}`,
  authApprove: "/api/auth/approve",
  authDevices: "/api/auth/devices",
  authDevice: (id: string) => `/api/auth/devices/${encodeURIComponent(id)}`,
  // Profiles (#297).
  authProfiles: "/api/auth/profiles",
  authProfile: (name: string) => `/api/auth/profiles/${encodeURIComponent(name)}`,
  authPick: "/api/auth/pick",
} as const;
