import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson, errorText, paths, type AppActionResult, type AppStatus, type AppUi, type Favorites } from "../api.ts";
import {
  APP_UI_WAIT_ATTEMPTS,
  APP_UI_WAIT_INTERVAL_MS,
  actionResultMessage,
  appUiUrl,
  normalizeFavorites,
  toggleFavoriteList,
} from "../logic/apps.ts";
import type { LoadingStep } from "../logic/loading.ts";

export interface PanelMessage {
  text: string;
  kind: "" | "ok" | "error";
}

/** An app's UI shown inside the dashboard (#159): its loading steps until the
 *  app's server answers, then the app itself. */
export interface AppView {
  name: string;
  display: string;
  /** The app's UI, once its server answers. */
  url: string | null;
  step: LoadingStep;
  error: string | null;
}

/** App states in which the view stays open. */
const APP_VIEW_LIVE = new Set(["running", "loading"]);

export interface AppsState {
  statuses: AppStatus[];
  /** Installed app names, in daemon order. */
  names: string[];
  favorites: string[];
  favoritesSaving: boolean;
  appBusy: boolean;
  /** Increments every poll; picks the loading-text fragment. */
  tick: number;
  message: PanelMessage;
  libraryHint: PanelMessage;
  poll: () => Promise<void>;
  loadFavorites: () => Promise<void>;
  /** Load the acting profile's pins afresh (a shared tablet switched profile). */
  reloadFavorites: () => Promise<void>;
  favoritesLoaded: () => boolean;
  toggleFavorite: (name: string) => Promise<void>;
  /** Starts or stops an app; starting one with a UI opens the app view. */
  perform: (name: string, action: "start" | "stop") => void;
  actionError: { name: string; text: string } | null;
  openUi: (name: string) => void;
  /** The app UI open inside the dashboard, if any. */
  appView: AppView | null;
  closeAppView: () => void;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const APP_VIEW_STATE = "reachyBridgeAppView";

export function useApps(): AppsState {
  const [statuses, setStatuses] = useState<AppStatus[]>([]);
  const [favorites, setFavorites] = useState<string[]>([]);
  const [favoritesSaving, setFavoritesSaving] = useState(false);
  const [appBusy, setAppBusy] = useState(false);
  const [tick, setTick] = useState(0);
  const [message, setMessage] = useState<PanelMessage>({ text: "", kind: "" });
  // The last failed stop: the status poll clears `message`, so this outlives it.
  const [actionError, setActionError] = useState<{ name: string; text: string } | null>(null);
  const [libraryHint, setLibraryHint] = useState<PanelMessage>({ text: "", kind: "" });

  // Refs mirror state that async flows read after awaits.
  const statusesRef = useRef<AppStatus[]>([]);
  const namesRef = useRef<string[]>([]);
  const favoritesRef = useRef<string[]>([]);
  const loadedRef = useRef(false);
  const loadingRef = useRef(false);
  const savingRef = useRef(false);
  const appBusyRef = useRef(false);
  const pollInflight = useRef(false);

  const writeFavorites = (next: string[]) => {
    favoritesRef.current = next;
    setFavorites(next);
  };

  const poll = useCallback(async () => {
    if (pollInflight.current) return;
    pollInflight.current = true;
    try {
      setTick((t) => t + 1);
      const result = await apiJson<AppStatus[]>(paths.appsStatus);
      if (Array.isArray(result)) {
        namesRef.current = result.map((app) => app.name);
        statusesRef.current = result;
        setStatuses(result);
        setMessage({ text: "", kind: "" });
      }
    } catch (err) {
      setMessage({ text: errorText(err, "App status unavailable"), kind: "error" });
    } finally {
      pollInflight.current = false;
    }
  }, []);

  // Bumped when the acting profile changes, so an older answer is dropped.
  const favoritesGen = useRef(0);
  const loadFavorites = useCallback(async () => {
    if (loadedRef.current || loadingRef.current) return;
    loadingRef.current = true;
    const gen = favoritesGen.current;
    try {
      const payload = await apiJson<Favorites>(paths.appsFavorites);
      if (gen !== favoritesGen.current) return;
      if (!payload || typeof payload !== "object" || !Array.isArray(payload.favorites)) {
        throw new Error("Favorites response was invalid");
      }
      writeFavorites(normalizeFavorites(payload.favorites, namesRef.current));
      loadedRef.current = true;
      setLibraryHint({ text: "", kind: "" });
    } catch (err) {
      // Leave `loaded` false so a later attempt (opening the library, a star
      // click) retries instead of PUTting defaults over real favorites.
      if (gen !== favoritesGen.current) return;
      setLibraryHint({ text: errorText(err, "Favorites unavailable; showing defaults."), kind: "error" });
    } finally {
      if (gen === favoritesGen.current) loadingRef.current = false;
    }
  }, []);

  /** Another profile acts now (a shared tablet's pick, #297): its own pins. */
  const reloadFavorites = useCallback(async () => {
    favoritesGen.current += 1;
    loadedRef.current = false;
    loadingRef.current = false;
    writeFavorites([]);
    await loadFavorites();
  }, [loadFavorites]);

  const toggleFavorite = useCallback(
    async (name: string) => {
      if (savingRef.current || !namesRef.current.includes(name)) return;
      if (!loadedRef.current) {
        await loadFavorites();
        if (!loadedRef.current) {
          setLibraryHint({ text: "Favorites unavailable; try again.", kind: "error" });
          return;
        }
      }
      const previous = [...favoritesRef.current];
      // Count and save only installed pins: the controller refuses unknown names.
      const toggled = toggleFavoriteList(normalizeFavorites(previous, namesRef.current), name);
      if (!toggled.ok) {
        setLibraryHint({ text: toggled.error, kind: "error" });
        return;
      }
      const next = toggled.next;
      writeFavorites(next);
      savingRef.current = true;
      setFavoritesSaving(true);
      try {
        await apiJson(paths.appsFavorites, { method: "PUT", body: JSON.stringify({ favorites: next }) });
        setLibraryHint({ text: "", kind: "" });
      } catch (err) {
        writeFavorites(previous);
        setLibraryHint({ text: errorText(err, "Could not save favorites."), kind: "error" });
      } finally {
        savingRef.current = false;
        setFavoritesSaving(false);
      }
    },
    [loadFavorites],
  );

  const [appView, setAppViewState] = useState<AppView | null>(null);
  const viewRef = useRef<AppView | null>(null);
  // Bumped whenever a view opens or closes, so a wait started for one view
  // can't reopen or change a later one.
  const viewSeq = useRef(0);
  // The view's app has been seen running or loading; only then does it
  // leaving those states close the view (a stale status can't).
  const seenLive = useRef(false);
  // history.back() was called and its popstate hasn't arrived yet.
  const backPending = useRef(false);

  const setView = (next: AppView | null) => {
    viewRef.current = next;
    setAppViewState(next);
  };

  /** Open the view of an app's UI; the view's sequence number. The view gets
   *  its own history entry, so Back (or a swipe) returns to the dashboard. */
  const openView = useCallback((name: string, patch: Partial<AppView>): number => {
    const current = viewRef.current;
    if (current?.name === name) {
      setView({ ...current, ...patch });
      return viewSeq.current;
    }
    viewSeq.current += 1;
    seenLive.current = false;
    const app = statusesRef.current.find((item) => item.name === name);
    setView({ name, display: app?.display || name, url: null, step: "waking", error: null, ...patch });
    // While a Back is in flight the entry is pushed when it lands.
    if (!backPending.current) {
      if (window.history.state?.[APP_VIEW_STATE]) window.history.replaceState({ [APP_VIEW_STATE]: name }, "");
      else window.history.pushState({ [APP_VIEW_STATE]: name }, "");
    }
    return viewSeq.current;
  }, []);

  /** Change the view, if it's still the one numbered `seq`. */
  const updateView = useCallback((seq: number, patch: Partial<AppView>) => {
    if (seq === viewSeq.current && viewRef.current) setView({ ...viewRef.current, ...patch });
  }, []);

  /** Back to the dashboard. The app keeps running. */
  const closeAppView = useCallback(() => {
    if (!viewRef.current) return;
    viewSeq.current += 1;
    setView(null);
    if (window.history.state?.[APP_VIEW_STATE] && !backPending.current) {
      backPending.current = true;
      window.history.back();
    }
  }, []);

  useEffect(() => {
    // A reload keeps the view's history entry but not the view.
    if (window.history.state?.[APP_VIEW_STATE]) window.history.replaceState(null, "");
    const onPop = () => {
      const view = viewRef.current;
      if (backPending.current) {
        backPending.current = false;
        // Reopened before the Back landed: give the new view its entry.
        if (view) window.history.pushState({ [APP_VIEW_STATE]: view.name }, "");
      } else if (!window.history.state?.[APP_VIEW_STATE]) {
        if (view) {
          viewSeq.current += 1;
          setView(null);
        }
      } else if (!view) {
        // Forward onto a closed view's entry: it's the dashboard now.
        window.history.replaceState(null, "");
      }
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // Close the view once its app stops or crashes (the Apps panel says which).
  // A view showing a start failure stays until it's dismissed.
  useEffect(() => {
    if (!appView || appView.error) return;
    const app = statuses.find((item) => item.name === appView.name);
    if (app && APP_VIEW_LIVE.has(app.status)) seenLive.current = true;
    else if (seenLive.current) closeAppView();
  }, [statuses, appView, closeAppView]);

  /** Wait for the view's app to serve its UI, then show it. */
  const showWhenReady = useCallback(
    async (seq: number, name: string) => {
      const crashed = () => statusesRef.current.find((item) => item.name === name)?.status === "error";
      let ready = false;
      let proxyPort: number | null | undefined;
      for (let attempt = 0; attempt < APP_UI_WAIT_ATTEMPTS; attempt += 1) {
        if (seq !== viewSeq.current) return;
        if (crashed()) return closeAppView();
        try {
          const payload = await apiJson<AppUi>(paths.appUi(name));
          if (payload && payload.port && payload.ready) {
            ready = true;
            proxyPort = payload.proxy_port;
            break;
          }
        } catch {
          // keep waiting
        }
        await sleep(APP_UI_WAIT_INTERVAL_MS);
      }
      // :8043 (tailscale serve) forwards to the same listener.
      if (ready && proxyPort === null) {
        updateView(seq, { error: "App interface unavailable" });
      } else if (!ready) {
        const display = viewRef.current?.display || name;
        updateView(seq, { error: `${display} didn't open within 30 s` });
      } else {
        updateView(seq, { step: "opening", url: appUiUrl(window.location.hostname, proxyPort ?? undefined) });
      }
    },
    [closeAppView, updateView],
  );

  const perform = useCallback(
    (name: string, action: "start" | "stop") => {
      if (appBusyRef.current) return;
      const app = statusesRef.current.find((item) => item.name === name);
      const withUi = action === "start" && Boolean(app?.has_ui);
      const seq = withUi ? openView(name, { url: null, step: "waking", error: null }) : 0;
      // Stopping from the view goes straight back to the dashboard.
      if (action === "stop" && viewRef.current?.name === name) closeAppView();
      appBusyRef.current = true;
      setAppBusy(true);
      setActionError(null);
      setMessage({ text: `${action === "start" ? "starting" : "stopping"} ${name}`, kind: "" });
      void (async () => {
        try {
          const result = await apiJson<AppActionResult>(
            action === "start" ? paths.appStart(name) : paths.appStop(name),
            { method: "POST" },
          );
          if (result && (result.status === "refused" || result.error)) {
            throw new Error(result.error || "Request refused");
          }
          await poll();
          setMessage({ text: actionResultMessage(name, result?.status), kind: "ok" });
          if (withUi && result && result.ui_port) {
            updateView(seq, { step: "starting" });
            void showWhenReady(seq, name);
          } else if (withUi && seq === viewSeq.current) {
            closeAppView();
          }
        } catch (err) {
          const text = errorText(err, "App request failed");
          if (withUi) updateView(seq, { error: text });
          setMessage({ text, kind: "error" });
          if (action === "stop") setActionError({ name, text });
          await poll();
        } finally {
          appBusyRef.current = false;
          setAppBusy(false);
        }
      })();
    },
    [poll, openView, updateView, closeAppView, showWhenReady],
  );

  const openUi = useCallback(
    (name: string) => {
      if (viewRef.current?.name === name) return;
      const app = statusesRef.current.find((item) => item.name === name);
      const seq = openView(name, { step: app?.status === "running" ? "opening" : "starting" });
      void showWhenReady(seq, name);
    },
    [openView, showWhenReady],
  );

  const names = statuses.map((app) => app.name);
  return {
    appView,
    closeAppView,
    statuses,
    names,
    // An update uninstalls and reinstalls the app, so a pin missing from the
    // installed list is hidden, not dropped, and comes back with the app.
    favorites: normalizeFavorites(favorites, names),
    favoritesSaving,
    appBusy,
    tick,
    message,
    libraryHint,
    poll,
    loadFavorites,
    reloadFavorites,
    favoritesLoaded: () => loadedRef.current,
    toggleFavorite,
    perform,
    actionError,
    openUi,
  };
}
