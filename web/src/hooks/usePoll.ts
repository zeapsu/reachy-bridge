import { useEffect, useRef } from "react";

/**
 * Run `task` now and every `intervalMs` while `enabled`. A tick is skipped
 * while the previous run is still in flight, as main.js's inflight flags did.
 */
export function usePoll(task: () => Promise<unknown>, intervalMs: number, enabled: boolean): void {
  const taskRef = useRef(task);
  taskRef.current = task;

  useEffect(() => {
    if (!enabled) return;
    let inflight = false;
    const run = async () => {
      if (inflight) return;
      inflight = true;
      try {
        await taskRef.current();
      } catch {
        // tasks report their own errors
      } finally {
        inflight = false;
      }
    };
    void run();
    const timer = setInterval(() => void run(), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs, enabled]);
}
