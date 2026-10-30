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
 * Control (#221). Polls while the page is visible: the poll is also how
 * the controller knows the holder is still here, so a hidden tab lets
 * control go after about 10 s (and takes it back on return within 5 min,
 * unless someone else took it). Closing or reloading the page releases
 * control at once.
 */
export function useControl(visible: boolean): ControlApi {
  const [view, setView] = useState<ControlView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const latest = useRef<ControlView | null>(null);
  const asked = useRef(0);
  const applied = useRef(0);

  const reload = useCallback(async () => {
    // A poll sent before a take can land after it: never apply an answer
    // older than one already shown (a slow link still gets every newer one).
    const seq = ++asked.current;
    try {
      const next = await apiJson<ControlView>(paths.control);
      if (seq < applied.current) return;
      applied.current = seq;
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

  // Closing, reloading or leaving the page releases control (not hiding it,
  // nor a page kept in the back/forward cache: those wait out the quiet
  // timeout and come back), unless another dashboard tab of this browser,
  // which shares its control, is still open. keepalive: the request
  // outlives the page.
  useEffect(() => {
    const me = Math.random().toString(36).slice(2);
    const others = new Set<string>();
    const channel = typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel("reachy-control-tabs");
    if (channel) {
      channel.onmessage = ({ data }: MessageEvent<{ kind: string; tab: string }>) => {
        if (data.kind === "bye") others.delete(data.tab);
        else others.add(data.tab);
        if (data.kind === "hello") channel.postMessage({ kind: "here", tab: me });
      };
      channel.postMessage({ kind: "hello", tab: me });
    }
    const leave = (event: PageTransitionEvent) => {
      channel?.postMessage({ kind: "bye", tab: me });
      if (event.persisted || others.size > 0 || !latest.current?.holder?.you) return;
      void fetch(paths.controlRelease, { method: "POST", keepalive: true }).catch(() => {});
    };
    // Back from the back/forward cache: byes sent meanwhile were missed, so
    // ask again who's open.
    const show = (event: PageTransitionEvent) => {
      if (!event.persisted) return;
      others.clear();
      channel?.postMessage({ kind: "hello", tab: me });
    };
    window.addEventListener("pagehide", leave);
    window.addEventListener("pageshow", show);
    return () => {
      window.removeEventListener("pagehide", leave);
      window.removeEventListener("pageshow", show);
      channel?.postMessage({ kind: "bye", tab: me });
      channel?.close();
    };
  }, []);

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
      // Nobody in control, or this person on another device (the server checks both again).
      const state = controlState(latest.current);
      if (!(state.kind === "free" && state.canTake) && !(state.kind === "other" && state.elsewhere !== undefined)) return false;
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
