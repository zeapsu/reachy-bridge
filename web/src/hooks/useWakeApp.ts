import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson, errorText, paths, type WakeApp } from "../api.ts";

export interface WakeAppState {
  /** Null until it loads, and when the controller doesn't answer. */
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
  // Bumped by every read and by a write's answer, so an older answer can't land over a newer one.
  const seq = useRef(0);
  // Bumped when a write ends: the switch is read again, whatever was read meanwhile.
  const [written, setWritten] = useState(0);

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
  }, [live, target, online, written]);

  const set = useCallback((enabled: boolean) => {
    setBusy(true);
    setError("");
    // One write at a time (the switch is disabled meanwhile), so its answer is the newest there is:
    // a read sent while it was on its way may have been answered from before the save.
    apiJson<WakeApp>(paths.wakeApp, { method: "PUT", body: JSON.stringify({ enabled }) })
      .then((next) => {
        ++seq.current;
        setInfo(next);
      })
      .catch((err) => setError(errorText(err, "Couldn't save the setting")))
      .finally(() => {
        setBusy(false);
        setWritten((n) => n + 1);
      });
  }, []);

  return { info, busy, error, set };
}
