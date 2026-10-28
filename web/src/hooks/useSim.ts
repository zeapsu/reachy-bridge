import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson, errorText, paths, setBelievedTarget, type SimStatus, type Target } from "../api.ts";
import { syncTarget } from "../logic/sim.ts";

export interface SimState {
  /** What this page believes the controller drives; null until the first poll. */
  target: Target | null;
  switching: boolean;
  error: string;
  toggle: () => void;
}

/**
 * Sim mode (#149). `reported` is the target from the 1 Hz power poll; a
 * switch made in another tab reloads this one, so it never shows (or acts
 * on) one robot while the controller drives the other.
 */
export function useSim(reported: Target | undefined, onSwitched: () => void): SimState {
  const [target, setTarget] = useState<Target | null>(null);
  const [switching, setSwitching] = useState(false);
  const [error, setError] = useState("");
  const switchingRef = useRef(false);
  // The target this page switched to, until a poll reports it.
  const pendingRef = useRef<Target | null>(null);
  const onSwitchedRef = useRef(onSwitched);
  onSwitchedRef.current = onSwitched;

  const believe = useCallback((next: Target) => {
    setBelievedTarget(next);
    setTarget(next);
  }, []);

  useEffect(() => {
    if (pendingRef.current !== null && reported === pendingRef.current) pendingRef.current = null;
    const decision = syncTarget(target, reported, switchingRef.current, pendingRef.current);
    if (decision === "adopt" && reported) believe(reported);
    if (decision === "reload") window.location.reload();
  }, [target, reported, believe]);

  const toggle = useCallback(() => {
    if (switchingRef.current || target === null) return;
    const entering = target === "robot";
    if (entering && !window.confirm("Switch the dashboard to a simulated Reachy? The real robot stays off until you switch back.")) return;
    switchingRef.current = true;
    setSwitching(true);
    setError("");
    void (async () => {
      try {
        const status = await apiJson<SimStatus>(entering ? paths.simEnter : paths.simExit, { method: "POST" });
        pendingRef.current = status.target;
        // A poll will report it within a second or two; if the controller
        // restarted or another tab switched back meanwhile, stop waiting so
        // the next poll reloads this tab.
        window.setTimeout(() => {
          if (pendingRef.current === status.target) pendingRef.current = null;
        }, 5000);
        believe(status.target);
        onSwitchedRef.current();
      } catch (err) {
        setError(errorText(err, entering ? "Couldn't start the simulation" : "Couldn't leave the simulation"));
      } finally {
        switchingRef.current = false;
        setSwitching(false);
      }
    })();
  }, [target, believe]);

  return { target, switching, error, toggle };
}
