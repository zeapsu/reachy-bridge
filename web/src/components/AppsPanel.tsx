import { useState, type CSSProperties, type ReactNode } from "react";
import type { AppStatus } from "../api.ts";
import { useLens } from "../glass.ts";
import type { AppsState } from "../hooks/useApps.ts";
import type { CatalogState } from "../hooks/useCatalog.ts";
import { appMeta } from "../logic/catalog.ts";
import { AppCopy, AppDetails, JobText } from "./CatalogPanel.tsx";
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
  catalog: CatalogState;
  /** Manage mode: Update and Remove instead of Start and the star. */
  manage: boolean;
}

/** One app on one line (#100): tile, name and state, its action, a star. */
function AppRow({ app, gates, favorite, saving, tick, now, apps, catalog, manage }: RowProps) {
  const status = cardStatus(app);
  const name = String(app.name);
  const display = app.display || name;
  const action = appAction(app, gates);

  const job = catalog.jobs[name];
  const jobBusy = Boolean(job && job.status !== "failed");
  const idle = status.kind === "" || status.kind === "error";
  let state: ReactNode;
  if (job) {
    state = <JobText job={job} />;
  } else if (status.kind === "running") {
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
  } else if (status.kind === "error" || app.error) {
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
      <AppCopy
        title={<span className="title">{display}</span>}
        state={state}
        details={<AppDetails description={state === app.blurb ? "" : app.blurb} meta={appMeta(app)} space={app.space} />}
      />
      <div className="app-actions">
        {manage ? (
          <>
            {catalog.updates.includes(name) ? (
              <button className="btn primary press" type="button" disabled={jobBusy || !idle} onClick={() => catalog.update(name)}>
                Update
              </button>
            ) : null}
            <button
              className="btn danger press"
              type="button"
              disabled={jobBusy || !idle}
              title={idle ? undefined : "Stop the app first"}
              onClick={() => {
                if (window.confirm(`Remove ${display}? You can install it again from Get apps.`)) catalog.remove(name);
              }}
            >
              Remove
            </button>
          </>
        ) : action.kind === "running" ? (
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
            disabled={action.disabled || jobBusy}
            title={(jobBusy ? `${job?.kind === "remove" ? "Removing" : "Updating"}…` : action.title) || undefined}
            onClick={() => apps.perform(name, "start")}
          >
            Start
          </button>
        )}
        {manage ? null : (
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
        )}
      </div>
    </li>
  );
}

interface Props {
  apps: AppsState;
  catalog: CatalogState;
  daemonOnline: boolean;
  /** The daemon's app API answers (the robot may be off). */
  reachable: boolean;
  exprBusy: boolean;
}

/**
 * Installed apps as compact rows. Pinned apps (favorites, plus whichever app
 * owns the robot or failed) stay on top; everything installed is below.
 */
export function AppsPanel({ apps, catalog, daemonOnline, reachable, exprBusy }: Props) {
  const sheet = useLens<HTMLElement>();
  const [manage, setManage] = useState(false);
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
      catalog={catalog}
      manage={manage}
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
        {apps.names.length ? (
          <button className="btn quiet press" type="button" aria-pressed={manage} onClick={() => setManage(!manage)}>
            {manage ? "Done" : catalog.updates.length ? `Manage · ${catalog.updates.length} update${catalog.updates.length > 1 ? "s" : ""}` : "Manage"}
          </button>
        ) : null}
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
        <p className="note">{reachable ? "No apps installed yet." : "Apps show up when the daemon is online."}</p>
      ) : null}
    </section>
  );
}
