import { useCallback, useRef, useState } from "react";
import { apiJson, errorText, paths, type AppActionResult, type AppStatus, type AppUi, type Favorites } from "../api.ts";
import {
  APP_UI_WAIT_ATTEMPTS,
  APP_UI_WAIT_INTERVAL_MS,
  actionResultMessage,
  appUiUrl,
  normalizeFavorites,
  toggleFavoriteList,
} from "../logic/apps.ts";
import { loadingHash, loadingPageUrl } from "../logic/loading.ts";

export interface PanelMessage {
  text: string;
  kind: "" | "ok" | "error";
}

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
  favoritesLoaded: () => boolean;
  toggleFavorite: (name: string) => Promise<void>;
  /** Must be called synchronously from the click (it may open a tab). */
  perform: (name: string, action: "start" | "stop") => void;
  openUi: (name: string) => void;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Drive the loading tab through its steps (app-loading.html reads the hash). */
function setLoadingStep(target: Window | null, step: string, attempt = 0): void {
  if (!target || target.closed) return;
  try {
    // Until the loading page has replaced the initial about:blank, a hash
    // change could cancel that navigation; wait for it instead.
    if (target.location.pathname !== paths.loadingPage) {
      if (attempt < 50) setTimeout(() => setLoadingStep(target, step, attempt + 1), 100);
      return;
    }
    target.location.hash = loadingHash(step);
  } catch {
    // The tab already navigated to the app's own origin.
  }
}

function openAppUiTab(port: number | string): void {
  window.open(appUiUrl(window.location.hostname, port), "_blank", "noopener");
}

async function waitAndOpenAppUi(name: string, target: Window | null): Promise<void> {
  for (let attempt = 0; attempt < APP_UI_WAIT_ATTEMPTS; attempt += 1) {
    try {
      const payload = await apiJson<AppUi>(paths.appUi(name));
      if (payload && payload.port && payload.ready) {
        setLoadingStep(target, "opening");
        if (target && !target.closed) target.location.href = appUiUrl(window.location.hostname, payload.port);
        else openAppUiTab(payload.port);
        return;
      }
    } catch {
      // keep waiting
    }
    await sleep(APP_UI_WAIT_INTERVAL_MS);
  }
  setLoadingStep(target, `failed:${name} did not open its interface within 30 s.`);
}

export function useApps(): AppsState {
  const [statuses, setStatuses] = useState<AppStatus[]>([]);
  const [favorites, setFavorites] = useState<string[]>([]);
  const [favoritesSaving, setFavoritesSaving] = useState(false);
  const [appBusy, setAppBusy] = useState(false);
  const [tick, setTick] = useState(0);
  const [message, setMessage] = useState<PanelMessage>({ text: "", kind: "" });
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
        // An app uninstalled since favorites loaded must not keep a slot: it
        // would count toward the limit but never render.
        const pruned = normalizeFavorites(favoritesRef.current, namesRef.current);
        if (pruned.length !== favoritesRef.current.length) writeFavorites(pruned);
        setStatuses(result);
        setMessage({ text: "", kind: "" });
      }
    } catch (err) {
      setMessage({ text: errorText(err, "App status unavailable"), kind: "error" });
    } finally {
      pollInflight.current = false;
    }
  }, []);

  const loadFavorites = useCallback(async () => {
    if (loadedRef.current || loadingRef.current) return;
    loadingRef.current = true;
    try {
      const payload = await apiJson<Favorites>(paths.appsFavorites);
      if (!payload || typeof payload !== "object" || !Array.isArray(payload.favorites)) {
        throw new Error("Favorites response was invalid");
      }
      writeFavorites(normalizeFavorites(payload.favorites, namesRef.current));
      loadedRef.current = true;
      setLibraryHint({ text: "", kind: "" });
    } catch (err) {
      // Leave `loaded` false so a later attempt (opening the library, a star
      // click) retries instead of PUTting defaults over real favorites.
      setLibraryHint({ text: errorText(err, "Favorites unavailable; showing defaults."), kind: "error" });
    } finally {
      loadingRef.current = false;
    }
  }, []);

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
      const toggled = toggleFavoriteList(previous, name);
      if (!toggled.ok) {
        setLibraryHint({ text: toggled.error, kind: "error" });
        return;
      }
      const next = normalizeFavorites(toggled.next, namesRef.current);
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

  const perform = useCallback(
    (name: string, action: "start" | "stop") => {
      if (appBusyRef.current) return;
      const app = statusesRef.current.find((item) => item.name === name);
      // Open the tab inside the click (popup blockers), then point it at the
      // app once its server answers.
      let pending: Window | null = null;
      if (action === "start" && app?.has_ui) {
        pending = window.open(loadingPageUrl(paths.loadingPage, app.display || name), "_blank");
        if (pending) pending.opener = null;
      }
      appBusyRef.current = true;
      setAppBusy(true);
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
          if (action === "start" && result && result.ui_port) {
            setLoadingStep(pending, "starting");
            void waitAndOpenAppUi(name, pending);
          } else if (pending) pending.close();
          await poll();
          setMessage({ text: actionResultMessage(name, result?.status), kind: "ok" });
        } catch (err) {
          const text = errorText(err, "App request failed");
          setLoadingStep(pending, `failed:${text}`);
          setMessage({ text, kind: "error" });
          await poll();
        } finally {
          appBusyRef.current = false;
          setAppBusy(false);
        }
      })();
    },
    [poll],
  );

  const openUi = useCallback((name: string) => {
    void (async () => {
      try {
        const payload = await apiJson<AppUi>(paths.appUi(name));
        if (payload && payload.port) openAppUiTab(payload.port);
      } catch (err) {
        setMessage({ text: errorText(err, "App UI unavailable"), kind: "error" });
      }
    })();
  }, []);

  return {
    statuses,
    names: statuses.map((app) => app.name),
    favorites,
    favoritesSaving,
    appBusy,
    tick,
    message,
    libraryHint,
    poll,
    loadFavorites,
    favoritesLoaded: () => loadedRef.current,
    toggleFavorite,
    perform,
    openUi,
  };
}
