import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson, errorText, paths, type ExpressionCurrent } from "../api.ts";
import { EXPRESSION_POLL_MS, MOVE_DATASETS, parseMoveList, type ExprBusyReason } from "../logic/expressions.ts";

const LOAD_RETRY_MS = 1000;

export interface ExpressionsState {
  /** Move names per dataset id, in MOVE_DATASETS order. */
  moves: Record<string, string[]>;
  loaded: boolean;
  busy: ExprBusyReason;
  /** The chip shown as playing. */
  playing: { dataset: string | null; move: string } | null;
  cancelHidden: boolean;
  cancelDisabled: boolean;
  error: string | null;
  play: (dataset: string, move: string) => void;
  cancel: () => void;
  /** Motor re-enable kicked a backend return-home session (#67): mirror it. */
  enterReturningHome: () => void;
}

/**
 * Expression lifecycle, ported from main.js: one move at a time, a single
 * 750 ms poller on /api/expressions/current drives the busy state until the
 * backend reports `active: false`, and any poll failure releases the UI.
 */
export function useExpressions(daemonOnline: boolean, appActive: boolean): ExpressionsState {
  const [moves, setMoves] = useState<Record<string, string[]>>({});
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<ExprBusyReason>(null);
  const [playing, setPlaying] = useState<ExpressionsState["playing"]>(null);
  const [cancelHidden, setCancelHidden] = useState(true);
  const [cancelDisabled, setCancelDisabled] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const busyRef = useRef(false);
  const poller = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPoller = useCallback((reconcile: boolean) => {
    if (poller.current !== null) {
      clearInterval(poller.current);
      poller.current = null;
    }
    if (reconcile) {
      busyRef.current = false;
      setBusy(null);
      setPlaying(null);
      setCancelHidden(true);
      setError(null);
    }
  }, []);

  const startPoller = useCallback(() => {
    // Only one poller at a time; busyRef already rejects concurrent plays.
    if (poller.current !== null) return;
    poller.current = setInterval(async () => {
      try {
        const current = await apiJson<ExpressionCurrent>(paths.expressionCurrent);
        if (!current || typeof current !== "object" || current.active === false) {
          stopPoller(true);
          return;
        }
        // Cancel only while playing; unknown phases hide it but keep polling.
        setCancelHidden(current.phase !== "playing");
      } catch {
        // Controller restart / network: release the UI rather than spin.
        stopPoller(true);
      }
    }, EXPRESSION_POLL_MS);
  }, [stopPoller]);

  useEffect(() => () => stopPoller(false), [stopPoller]);

  // A state change (offline, an app taking over) clears a stale play error.
  useEffect(() => setError(null), [daemonOnline, appActive]);

  // Populate the strips once the daemon is reachable; retry while it stays so.
  // Each run owns its fetch: a stream drop mid-load cancels it, and the next
  // run fetches again rather than waiting on one whose result it will drop.
  useEffect(() => {
    if (!daemonOnline || loaded) return;
    let cancelled = false;
    let retry: ReturnType<typeof setTimeout> | null = null;
    const load = async () => {
      try {
        const groups = await Promise.all(
          MOVE_DATASETS.map(async ({ id, dataset }) => ({
            id,
            moves: parseMoveList(await apiJson(paths.moveDatasetList(dataset))),
          })),
        );
        if (cancelled) return;
        setMoves(Object.fromEntries(groups.map((g) => [g.id, g.moves])));
        setLoaded(true);
      } catch {
        if (!cancelled) retry = setTimeout(() => void load(), LOAD_RETRY_MS);
      }
    };
    void load();
    return () => {
      cancelled = true;
      if (retry !== null) clearTimeout(retry);
    };
  }, [daemonOnline, loaded]);

  // Sync-on-load: a move may already be active (another tab, a reload
  // mid-playback). Enter the busy state and poll it out. Its own effect: the
  // load's setLoaded re-runs (and cancels) the load effect before this answer.
  useEffect(() => {
    if (!daemonOnline || !loaded || busyRef.current) return;
    let cancelled = false;
    void (async () => {
      try {
        const current = await apiJson<ExpressionCurrent>(paths.expressionCurrent);
        if (cancelled || busyRef.current || !current || typeof current !== "object" || current.active !== true) return;
        busyRef.current = true;
        setBusy("sync");
        setPlaying(
          current.phase === "playing" && current.move ? { dataset: current.dataset ?? null, move: current.move } : null,
        );
        setCancelHidden(current.phase !== "playing");
        startPoller();
      } catch {
        // leave the UI idle; a later poll reconciles
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [daemonOnline, loaded, startPoller]);

  const play = useCallback(
    async (dataset: string, move: string) => {
      if (busyRef.current) return;
      busyRef.current = true;
      setBusy("play");
      setPlaying({ dataset, move });
      setCancelHidden(false);
      setError(null);
      try {
        const result = await apiJson(paths.expressionPlay(dataset, move), { method: "POST" });
        if (result && typeof result === "object") {
          startPoller();
          return;
        }
        throw new Error("Invalid response");
      } catch (err) {
        // 409 carries "Robot is busy with another move." (or the app/homing reason).
        busyRef.current = false;
        setBusy(null);
        setPlaying(null);
        setCancelHidden(true);
        setCancelDisabled(false);
        setError(errorText(err, "Move failed"));
      }
    },
    [startPoller],
  );

  const cancel = useCallback(async () => {
    // Stop is idempotent server-side; polling continues until active === false.
    setCancelDisabled(true);
    try {
      await apiJson(paths.expressionStop, { method: "POST" });
    } catch {
      // the next poll reconciles
    } finally {
      setCancelDisabled(false);
    }
  }, []);

  const enterReturningHome = useCallback(() => {
    busyRef.current = true;
    setBusy("returning");
    setPlaying(null);
    setCancelHidden(true);
    setError(null);
    startPoller();
  }, [startPoller]);

  return {
    moves,
    loaded,
    busy,
    playing,
    cancelHidden,
    cancelDisabled,
    error,
    play: (dataset, move) => void play(dataset, move),
    cancel: () => void cancel(),
    enterReturningHome,
  };
}
