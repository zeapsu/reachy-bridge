import { useCallback, useEffect, useState } from "react";
import { apiJson, errorText, paths } from "../api.ts";
import type { AuthStatus, ProfileView } from "../logic/auth.ts";
import { rights, type Rights } from "../logic/profile.ts";

/** How often a profile is re-read, so an admin's change shows up (#297). */
const PROFILE_POLL_MS = 15_000;

export interface ProfileApi {
  profile: ProfileView | null;
  /** The first answer came back (until then System stays closed). */
  loaded: boolean;
  rights: Rights;
  /** Pick (or with null, hand back) the profile on a shared device. */
  pick: (person: string | null) => Promise<boolean>;
  error: string | null;
  reload: () => Promise<void>;
}

/** This browser's profile in family mode (#297), from /api/auth/status. */
export function useProfile(visible: boolean): ProfileApi {
  const [profile, setProfile] = useState<ProfileView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const reload = useCallback(async () => {
    try {
      const status = await apiJson<AuthStatus>(paths.authStatus);
      setProfile(status.profile ?? null);
      setLoaded(true);
    } catch {
      // Keep the last one: the controller enforces it anyway.
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    void reload();
    const timer = window.setInterval(() => void reload(), PROFILE_POLL_MS);
    return () => window.clearInterval(timer);
  }, [visible, reload]);

  const pick = useCallback(
    async (person: string | null) => {
      setError(null);
      try {
        await apiJson(paths.authPick, { method: "POST", body: JSON.stringify({ person }), headers: { "Content-Type": "application/json" } });
        await reload();
        return true;
      } catch (err) {
        setError(errorText(err, "Couldn't switch profile"));
        return false;
      }
    },
    [reload],
  );

  return { profile, loaded, rights: rights(profile), pick, error, reload };
}
