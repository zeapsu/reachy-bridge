import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson, errorText, paths, setControlTaker, type ControlView } from "../api.ts";
import { CONTROL_POLL_MS, controlState } from "../logic/control.ts";

export interface ControlApi {
  view: ControlView | null;
  error: string | null;
  busy: boolean;
  /** Run an action, then refresh; errors land in `error`. */
  act: (path: string, method?: string, body?: unknown) => Promise<boolean>;
  reload: () => Promise<void>;
}

/**
 * Control time (#221). Polls while the page is visible: the poll is also how
 * the controller knows the holder is still here, so a hidden tab lets
 * control go after about 10 s (and takes it back on return, within the
 * grant).
 */
export function useControl(visible: boolean): ControlApi {
  const [view, setView] = useState<ControlView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const latest = useRef<ControlView | null>(null);

  const reload = useCallback(async () => {
    try {
      const next = await apiJson<ControlView>(paths.control);
      latest.current = next;
      setView(next);
    } catch {
      // An older controller has no control routes; a blip retries next tick.
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    void reload();
    const timer = setInterval(() => void reload(), CONTROL_POLL_MS);
    return () => clearInterval(timer);
  }, [visible, reload]);

  const act = useCallback(
    async (path: string, method = "POST", body?: unknown) => {
      setBusy(true);
      setError(null);
      try {
        await apiJson(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
        return true;
      } catch (e) {
        setError(errorText(e, "That didn't work."));
        return false;
      } finally {
        setBusy(false);
        await reload();
      }
    },
    [reload],
  );

  // A robot action while nobody is in control takes it first (api/client.ts).
  useEffect(() => {
    setControlTaker(async () => {
      const state = controlState(latest.current);
      if (state.kind !== "free" || !state.canTake) return false;
      try {
        await apiJson(paths.controlTakeIfFree, { method: "POST" });
        return true;
      } catch {
        return false;
      } finally {
        await reload();
      }
    });
    return () => setControlTaker(null);
  }, [reload]);

  return { view, error, busy, act, reload };
}
