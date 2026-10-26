import type { CSSProperties, ReactNode } from "react";
import type { AppStatus } from "../api.ts";
import { useLens } from "../glass.ts";
import type { AppsState } from "../hooks/useApps.ts";
import {
  LOADING_FRAGMENTS,
  MAX_FAVORITES,
  activeApp,
  appAction,
  appForName,
  appHue,
  appInitial,
  cardStatus,
  uptimeSuffix,
  visibleAppNames,
  type ActionGates,
} from "../logic/apps.ts";
import "./AppsPanel.css";

interface RowProps {
  app: AppStatus;
  gates: ActionGates;
  favorite: boolean;
  saving: boolean;
  tick: number;
  now: number;
  apps: AppsState;
}

/** One app on one line (#100): tile, name and state, its action, a star. */
function AppRow({ app, gates, favorite, saving, tick, now, apps }: RowProps) {
  const status = cardStatus(app);
  const name = String(app.name);
  const display = app.display || name;
  const action = appAction(app, gates);

  let state: ReactNode;
  if (status.kind === "running") {
    state = (
      <>
        <span className="dot live"></span>Running{app.start_time ? uptimeSuffix(app, now) : ""}
      </>
    );
  } else if (status.kind === "loading") {
    state = (
      <>
        <span className="spin" aria-hidden="true"></span>
        {LOADING_FRAGMENTS[tick % LOADING_FRAGMENTS.length]}
      </>
    );
  } else if (status.kind === "stopping") {
    state = (
      <>
        <span className="spin" aria-hidden="true"></span>Stopping…
      </>
    );
  } else if (status.kind === "error") {
    state = (
      <span className="err">
        <span className="dot error"></span>
        {app.error || "Error"}
      </span>
    );
  } else {
    state = app.blurb || "Not running";
  }

  return (
    <li className={`app-row ${status.statusClass}`} data-app={name}>
      <span className="app-tile" style={{ "--hue": appHue(name) } as CSSProperties} aria-hidden="true">
        {appInitial(display)}
      </span>
      <div className="app-copy">
        <b>{display}</b>
        <small title={typeof state === "string" ? state : undefined}>{state}</small>
      </div>
      <div className="app-actions">
        {action.kind === "running" ? (
          <>
            {action.showOpen ? (
              <button className="btn primary press" type="button" title="Open the app's own interface" onClick={() => apps.openUi(name)}>
                Open
              </button>
            ) : null}
            <button className="btn danger press" type="button" disabled={action.stopDisabled} onClick={() => apps.perform(name, "stop")}>
              Stop
            </button>
          </>
        ) : action.kind === "pending" ? (
          <button className="btn" type="button" disabled>
            {action.label}
          </button>
        ) : (
          <button
            className="btn press"
            type="button"
            disabled={action.disabled}
            title={action.title || undefined}
            onClick={() => apps.perform(name, "start")}
          >
            Start
          </button>
        )}
        <button
          className={`star press${favorite ? " on" : ""}`}
          type="button"
          aria-pressed={favorite}
          disabled={saving}
          title={favorite ? "Unpin" : "Pin to the top"}
          aria-label={`${favorite ? "Unpin" : "Pin"} ${display}`}
          onClick={() => void apps.toggleFavorite(name)}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="m12 3.6 2.6 5.3 5.8.8-4.2 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.2-4.1 5.8-.8L12 3.6Z"></path>
          </svg>
        </button>
      </div>
    </li>
  );
}

interface Props {
  apps: AppsState;
  daemonOnline: boolean;
  exprBusy: boolean;
}

/**
 * Installed apps as compact rows. Pinned apps (favorites, plus whichever app
 * owns the robot or failed) stay on top; everything installed is below.
 */
export function AppsPanel({ apps, daemonOnline, exprBusy }: Props) {
  const sheet = useLens<HTMLElement>();
  const active = activeApp(apps.statuses);
  const gates: ActionGates = { anyActive: Boolean(active), appBusy: apps.appBusy, daemonOnline, exprBusy };
  const now = Date.now();
  const pinned = visibleAppNames(apps.names, apps.statuses, apps.favorites);
  const rest = apps.names.filter((name) => !pinned.includes(name));
  const row = (name: string) => (
    <AppRow
      key={name}
      app={appForName(apps.statuses, name)}
      gates={gates}
      favorite={apps.favorites.includes(name)}
      saving={apps.favoritesSaving}
      tick={apps.tick}
      now={now}
      apps={apps}
    />
  );

  return (
    <section className="glass sheet apps-sheet" data-lens ref={sheet} id="apps-panel" aria-labelledby="apps-title">
      <header>
        <h2 id="apps-title">Apps</h2>
        <span className="chip" title={`Pin up to ${MAX_FAVORITES} apps`}>
          {apps.favoritesSaving ? <span className="spin" aria-hidden="true"></span> : null}
          {apps.favorites.length}/{MAX_FAVORITES} pinned
        </span>
      </header>
      <p className={`note${apps.message.kind ? ` ${apps.message.kind}` : ""}`} id="apps-message" role="status">
        {apps.message.text}
      </p>
      <p className={`note${apps.libraryHint.kind ? ` ${apps.libraryHint.kind}` : ""}`} role="status">
        {apps.libraryHint.text}
      </p>
      {pinned.length ? (
        <ul className="app-list" aria-label="Pinned apps" aria-live="polite">
          {pinned.map(row)}
        </ul>
      ) : null}
      {rest.length ? (
        <>
          {pinned.length ? <span className="list-label">All apps</span> : null}
          <ul className="app-list" aria-label="All apps">
            {rest.map(row)}
          </ul>
        </>
      ) : null}
      {apps.names.length === 0 ? (
        <p className="note">{daemonOnline ? "No apps installed yet." : "Apps show up when the daemon is online."}</p>
      ) : null}
    </section>
  );
}
