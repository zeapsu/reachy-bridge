import { useEffect, useRef, useState, type ReactNode } from "react";
import type { AppStatus } from "../api.ts";
import type { AppsState } from "../hooks/useApps.ts";
import {
  LOADING_FRAGMENTS,
  MAX_FAVORITES,
  activeApp,
  appAction,
  appForName,
  cardStatus,
  uptimeSuffix,
  visibleAppNames,
  type ActionGates,
} from "../logic/apps.ts";

interface CardProps {
  app: AppStatus;
  gates: ActionGates;
  favorites: string[];
  library: boolean;
  saving: boolean;
  tick: number;
  now: number;
  apps: AppsState;
}

function AppCard({ app, gates, favorites, library, saving, tick, now, apps }: CardProps) {
  const status = cardStatus(app);
  const name = String(app.name);
  const display = app.display || name;
  const favorite = favorites.includes(name);
  const transient = !favorite && status.kind !== "";
  const action = appAction(app, gates);

  let stateText: ReactNode;
  if (status.kind === "running") {
    stateText = (
      <>
        <span className="up">running</span>
        {app.start_time ? <span className="app-uptime" data-app={name}>{uptimeSuffix(app, now)}</span> : null}
      </>
    );
  } else if (status.kind === "loading") {
    stateText = (
      <>
        <span className="spinner-glyph" aria-hidden="true"></span>
        <span className="loading-frag">{LOADING_FRAGMENTS[tick % LOADING_FRAGMENTS.length]}</span>
      </>
    );
  } else if (status.kind === "stopping") {
    stateText = (
      <>
        <span className="spinner-glyph" aria-hidden="true"></span>
        <span className="stopping-label">Stopping…</span>
      </>
    );
  } else {
    stateText = app.status || "stopped";
  }

  return (
    <div className={`app-card ${status.statusClass}${transient ? " transient" : ""}`} data-app={name}>
      <span className={`dot ${status.dotClass}`}></span>
      <div className="app-copy">
        <div className="app-name">
          {display} <span className="app-key">{name}</span>
        </div>
        {library ? (
          <div className="app-blurb">{app.blurb || "No description available."}</div>
        ) : app.blurb ? (
          <div className="app-blurb">{app.blurb}</div>
        ) : null}
        <div className="app-state">
          {stateText}
          {app.error ? (
            <>
              <br />
              <span className="err">{app.error}</span>
            </>
          ) : null}
          {transient ? (
            <span className="app-transient">{status.kind === "error" ? "error" : "active"} · not favorite</span>
          ) : null}
        </div>
      </div>
      <div className="app-actions">
        {action.kind === "running" ? (
          <>
            {action.showOpen ? (
              <button className="btn open" type="button" title="Open the app's own interface" onClick={() => apps.openUi(name)}>
                Open
              </button>
            ) : null}
            <button className="btn stop" type="button" disabled={action.stopDisabled} onClick={() => apps.perform(name, "stop")}>
              Stop
            </button>
          </>
        ) : action.kind === "pending" ? (
          <button className="btn pending" type="button" disabled>
            {action.label}
          </button>
        ) : (
          <button
            className="btn start"
            type="button"
            disabled={action.disabled}
            title={action.title || undefined}
            onClick={() => apps.perform(name, "start")}
          >
            Start
          </button>
        )}
        {library ? (
          <button
            className={`btn star${favorite ? " selected" : ""}`}
            type="button"
            aria-pressed={favorite}
            disabled={saving}
            title={favorite ? "Remove from favorites" : "Add to favorites"}
            aria-label={`${favorite ? "Remove" : "Add"} ${display} ${favorite ? "from" : "to"} favorites`}
            onClick={() => void apps.toggleFavorite(name)}
          >
            <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <path d="m12 3.6 2.6 5.3 5.8.8-4.2 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.2-4.1 5.8-.8L12 3.6Z"></path>
            </svg>
          </button>
        ) : null}
      </div>
    </div>
  );
}

interface Props {
  apps: AppsState;
  daemonOnline: boolean;
  exprBusy: boolean;
}

export function AppsPanel({ apps, daemonOnline, exprBusy }: Props) {
  const [libraryOpen, setLibraryOpen] = useState(false);
  const openButton = useRef<HTMLButtonElement>(null);
  const backButton = useRef<HTMLButtonElement>(null);
  const shownOpen = useRef(libraryOpen);

  const active = activeApp(apps.statuses);
  const gates: ActionGates = { anyActive: Boolean(active), appBusy: apps.appBusy, daemonOnline, exprBusy };
  const now = Date.now();
  const card = (app: AppStatus, library: boolean) => (
    <AppCard
      key={app.name}
      app={app}
      gates={gates}
      favorites={apps.favorites}
      library={library}
      saving={apps.favoritesSaving}
      tick={apps.tick}
      now={now}
      apps={apps}
    />
  );

  useEffect(() => {
    // Move focus only on an actual open/close, not on mount (or StrictMode re-runs).
    if (shownOpen.current === libraryOpen) return;
    shownOpen.current = libraryOpen;
    (libraryOpen ? backButton : openButton).current?.focus();
  }, [libraryOpen]);

  const toggleLibrary = () => {
    if (!libraryOpen && !apps.favoritesLoaded()) void apps.loadFavorites(); // retry a failed initial fetch
    setLibraryOpen(!libraryOpen);
  };

  const visible = visibleAppNames(apps.names, apps.statuses, apps.favorites);
  return (
    <section className="glass pad instrument" id="apps-panel">
      <div id="apps-view" hidden={libraryOpen}>
        <div className="head apps-head">
          <h2>apps</h2>
          <div className="rule"></div>
          <button
            className="btn library-open"
            id="app-library-open"
            ref={openButton}
            type="button"
            aria-expanded={libraryOpen}
            aria-controls="app-library"
            title="Open app library"
            onClick={toggleLibrary}
          >
            library
          </button>
        </div>
        <div className="apps" id="apps" aria-live="polite">
          {visible.map((name) => card(appForName(apps.statuses, name), false))}
        </div>
        <p className={`panel-hint${apps.message.kind ? ` ${apps.message.kind}` : ""}`} id="apps-message" role="status">
          {apps.message.text}
        </p>
      </div>
      <div id="app-library" hidden={!libraryOpen}>
        <div className="head">
          <button
            className="btn settings-back"
            id="app-library-back"
            ref={backButton}
            type="button"
            aria-label="Back to favorite apps"
            onClick={() => setLibraryOpen(false)}
          >
            ‹ favorites
          </button>
          <h2>library</h2>
          <span className="settings-app" id="app-library-count">
            {apps.favorites.length}/{MAX_FAVORITES} favorites
          </span>
          <div className="rule"></div>
          <span className="pill">
            <span className={`dot ${apps.favoritesSaving ? "loading" : "ok"}`} id="app-library-dot"></span>
            <span id="app-library-state">{apps.favoritesSaving ? "saving" : "ready"}</span>
          </span>
        </div>
        <div className="library" id="app-library-list" aria-live="polite">
          {libraryOpen ? apps.names.map((name) => card(appForName(apps.statuses, name), true)) : null}
        </div>
        <p
          className={`panel-hint${apps.libraryHint.kind ? ` ${apps.libraryHint.kind}` : ""}`}
          id="app-library-hint"
          role="status"
        >
          {apps.libraryHint.text}
        </p>
      </div>
    </section>
  );
}
