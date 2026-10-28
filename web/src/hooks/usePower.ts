import { useCallback, useRef, useState } from "react";
import { apiJson, errorText, paths, type DaemonStatus } from "../api.ts";
import { INITIAL_POWER, derivePower, type PowerView } from "../logic/power.ts";

export interface ControlFeedback {
  setControlBusy: (busy: boolean) => void;
  setControlStatus: (text: string) => void;
  setControlHint: (text: string, error?: boolean) => void;
}

export interface PowerState {
  view: PowerView;
  /** The daemon answers, whether or not the robot is on (its app API works either way). */
  reachable: boolean;
  poll: () => Promise<void>;
  toggle: () => void;
  /** Mark (or clear) a power-off started outside the button (kinematics switch). */
  setStopping: (stopping: boolean) => void;
}

/**
 * Robot power button: 1 Hz /api/daemon/status poll plus the on/off actions.
 * `onStatusChange` fires when the reported power status changes (the running
 * kinematics engine changes with power).
 */
export function usePower(feedback: ControlFeedback, onStatusChange: () => void): PowerState {
  const [view, setView] = useState<PowerView>(INITIAL_POWER);
  const [reachable, setReachable] = useState(false);
  const viewRef = useRef(view);
  const stopping = useRef(false);
  const stoppingSince = useRef(0);
  const lastStatus = useRef<string | null>(null);
  const inflight = useRef(false);
  const feedbackRef = useRef(feedback);
  feedbackRef.current = feedback;
  const onStatusChangeRef = useRef(onStatusChange);
  onStatusChangeRef.current = onStatusChange;

  const show = (next: PowerView) => {
    viewRef.current = next;
    setView(next);
  };

  const poll = useCallback(async () => {
    if (inflight.current) return;
    inflight.current = true;
    try {
      const status = await apiJson<DaemonStatus>(paths.daemonStatus);
      const derived = derivePower(status, stopping.current, stoppingSince.current, Date.now());
      stopping.current = derived.stopping;
      if (status.status !== lastStatus.current) {
        lastStatus.current = status.status;
        onStatusChangeRef.current();
      }
      show(derived.view);
      setReachable(status.reachable);
    } catch {
      // Silent: the telemetry stream owns online/offline.
      setReachable(false);
    } finally {
      inflight.current = false;
    }
  }, []);

  const setStopping = useCallback((value: boolean) => {
    stopping.current = value;
    if (value) stoppingSince.current = Date.now();
  }, []);

  const toggle = useCallback(() => {
    const current = viewRef.current;
    if (current.disabled) return;
    const fb = feedbackRef.current;
    if (current.powerClass === "running") {
      fb.setControlBusy(true);
      stopping.current = true;
      stoppingSince.current = Date.now();
      show({ known: true, powerClass: "stopping", title: "Shutting down…", disabled: true, label: "Shutting down…" });
      fb.setControlStatus("stopping");
      void (async () => {
        try {
          await apiJson(paths.daemonStop, { method: "POST" });
        } catch (err) {
          stopping.current = false;
          show({ ...viewRef.current, powerClass: "", label: "" });
          fb.setControlStatus("error");
          fb.setControlHint(errorText(err, "Failed to stop daemon"), true);
        } finally {
          fb.setControlBusy(false);
          void poll();
        }
      })();
    } else {
      fb.setControlBusy(true);
      fb.setControlStatus("starting");
      show({ ...current, powerClass: "starting", label: "Starting…", disabled: true });
      void (async () => {
        try {
          await apiJson(paths.daemonStart, { method: "POST" });
        } catch (err) {
          fb.setControlStatus("error");
          fb.setControlHint(errorText(err, "Failed to start daemon"), true);
          show({ ...current, label: "", disabled: false });
        } finally {
          fb.setControlBusy(false);
          void poll();
        }
      })();
    }
  }, [poll]);

  return { view, reachable, poll, toggle, setStopping };
}
