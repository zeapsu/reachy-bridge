import { useCallback, useRef, useState } from "react";
import { apiJson, errorText, paths, type DaemonStatus, type Target } from "../api.ts";
import { INITIAL_POWER, derivePower, type PowerView } from "../logic/power.ts";

export interface ControlFeedback {
  setControlBusy: (busy: boolean) => void;
  setControlStatus: (text: string) => void;
  setControlHint: (text: string, error?: boolean) => void;
}

export interface PowerState {
  view: PowerView;
  poll: () => Promise<void>;
  toggle: () => void;
  /** Mark (or clear) a power-off started outside the button (kinematics switch). */
  setStopping: (stopping: boolean) => void;
  /** The daemon the controller reported driving on the last poll (#149). */
  target: Target | undefined;
  /** The last poll's power status ("stopped", "running", ...). */
  status: string | null;
}

/**
 * Robot power button: 1 Hz /api/daemon/status poll plus the on/off actions.
 * `onStatusChange` fires when the reported power status changes (the running
 * kinematics engine changes with power).
 */
export function usePower(feedback: ControlFeedback, onStatusChange: () => void): PowerState {
  const [view, setView] = useState<PowerView>(INITIAL_POWER);
  const [target, setTarget] = useState<Target | undefined>(undefined);
  const [status, setStatus] = useState<string | null>(null);
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
      setTarget(status.target);
      setStatus(status.reachable ? status.status : "offline");
      stopping.current = derived.stopping;
      if (status.status !== lastStatus.current) {
        lastStatus.current = status.status;
        onStatusChangeRef.current();
      }
      show(derived.view);
    } catch {
      // Silent: the telemetry stream owns online/offline.
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

  return { view, poll, toggle, setStopping, target, status };
}
