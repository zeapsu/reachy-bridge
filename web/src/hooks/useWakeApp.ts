import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson, errorText, paths, type WakeApp } from "../api.ts";

export interface WakeAppState {
  /** Null until it loads, and while the robot service can't say what is installed. */
  info: WakeApp | null;
  busy: boolean;
  error: string;
  set: (enabled: boolean) => void;
}

/**
 * The app that starts when the robot wakes (#334). Read while `live`
 * (System › Settings is showing), so an install made in Apps shows on the way
 * back, again when `target` changes (the sim has its own switch), and when
 * the robot service comes back (`online`), since only it knows what is installed.
 */
export function useWakeApp(live: boolean, target: string | null, online: boolean): WakeAppState {
  const [info, setInfo] = useState<WakeApp | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Bumped by every read and write, so an older answer can't land over a newer one.
  const seq = useRef(0);

  useEffect(() => {
    if (!live) return;
    const mine = ++seq.current;
    apiJson<WakeApp>(paths.wakeApp)
      .then((next) => {
        if (mine === seq.current) setInfo(next);
      })
      .catch(() => {
        if (mine === seq.current) setInfo(null);
      });
  }, [live, target, online]);

  const set = useCallback((enabled: boolean) => {
    const mine = ++seq.current;
    setBusy(true);
    setError("");
    apiJson<WakeApp>(paths.wakeApp, { method: "PUT", body: JSON.stringify({ enabled }) })
      .then((next) => {
        if (mine === seq.current) setInfo(next);
      })
      .catch((err) => {
        if (mine === seq.current) setError(errorText(err, "Couldn't save the setting"));
      })
      // One write at a time (the switch is disabled meanwhile), so this is always the one that ended.
      .finally(() => setBusy(false));
  }, []);

  return { info, busy, error, set };
}
