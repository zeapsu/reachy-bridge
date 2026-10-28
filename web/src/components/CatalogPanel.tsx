import { useId, useState, type CSSProperties, type ReactNode } from "react";
import type { CatalogApp } from "../api.ts";
import { useLens } from "../glass.ts";
import type { CatalogState, RowJob } from "../hooks/useCatalog.ts";
import { appHue, appInitial } from "../logic/apps.ts";
import { appMeta, filterCatalog, spaceUrl, tokenShape, validSpaceId } from "../logic/catalog.ts";
import { Segmented } from "./Segmented.tsx";
import "./CatalogPanel.css";

const JOB_VERB = { install: "Installing", update: "Updating", remove: "Removing" } as const;

/** A row's job line: progress while it runs, the error if it failed. */
export function JobText({ job }: { job: RowJob }) {
  if (job.status === "failed") {
    return (
      <span className="err" title={job.line}>
        <span className="dot error"></span>
        {job.line}
      </span>
    );
  }
  return (
    <>
      <span className="spin" aria-hidden="true"></span>
      <span title={job.line}>{job.line || `${JOB_VERB[job.kind]}…`}</span>
    </>
  );
}

/** An app row's name and state; tapping it shows what the app does. */
export function AppCopy({ title, state, details }: { title: ReactNode; state: ReactNode; details: ReactNode }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <>
      <button type="button" className="app-copy" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
        <b>
          {title}
          <span className="more" aria-hidden="true"></span>
        </b>
        <small title={typeof state === "string" ? state : undefined}>{state}</small>
      </button>
      {open ? (
        <div className="app-details" id={id}>
          {details}
        </div>
      ) : null}
    </>
  );
}

/**
 * What an app does: who made it and its Space page (whose README explains
 * it), plus its description when the row isn't already showing it.
 */
export function AppDetails({ description, meta, space }: { description?: string; meta: string; space?: string | null }) {
  const url = spaceUrl(space);
  return (
    <>
      {description ? <p>{description}</p> : null}
      {meta ? <small>{meta}</small> : null}
      {url ? (
        <a className="btn quiet press" href={url} target="_blank" rel="noopener noreferrer">
          More on Hugging Face ↗
        </a>
      ) : (
        <small>Not installed from a Hugging Face Space, so there's no page for it.</small>
      )}
    </>
  );
}

function CatalogRow({ app, job, onInstall }: { app: CatalogApp; job?: RowJob; onInstall: () => void }) {
  const busy = job && job.status !== "failed";
  return (
    <li className="app-row" data-space={app.id}>
      <span className="app-tile" style={{ "--hue": appHue(app.name) } as CSSProperties} aria-hidden="true">
        {appInitial(app.title)}
      </span>
      <AppCopy
        title={
          <>
            <span className="title">{app.title}</span>
            {app.private ? <span className="chip private">private</span> : null}
          </>
        }
        state={job ? <JobText job={job} /> : app.description || app.id}
        details={<AppDetails description={job ? app.description : ""} meta={appMeta(app)} space={app.id} />}
      />
      <div className="app-actions">
        {app.installed ? (
          <span className="chip">Installed</span>
        ) : (
          <button className="btn press" type="button" disabled={busy} onClick={onInstall} aria-label={`Install ${app.title}`}>
            {busy ? "Installing…" : "Install"}
          </button>
        )}
      </div>
    </li>
  );
}

function HfLoginRow({ catalog }: { catalog: CatalogState }) {
  const [token, setToken] = useState("");
  const [space, setSpace] = useState("");
  const { hf } = catalog;
  const submitToken = async (event: { preventDefault(): void }) => {
    event.preventDefault();
    // Cleared either way: a rejected token shouldn't sit in the page.
    setToken("");
    await catalog.saveToken(token);
  };
  const submitSpace = (event: { preventDefault(): void }) => {
    event.preventDefault();
    catalog.installPrivate(space);
    setSpace("");
  };
  const privateJob = Object.entries(catalog.jobs).find(([key]) => key.includes("/"));

  return (
    <div className="group hf">
      <span className="label">Hugging Face</span>
      {hf?.logged_in ? (
        <>
          <div className="row">
            <span>
              Signed in{hf.username ? ` as ${hf.username}` : ""}. Private Spaces you can see are listed under All Spaces.
            </span>
            <button className="btn quiet press" type="button" disabled={catalog.hfBusy} onClick={catalog.signOut}>
              Sign out
            </button>
          </div>
          <form className="inline-form" onSubmit={submitSpace}>
            <input
              className="field"
              type="text"
              placeholder="owner/space"
              aria-label="Private Space id"
              autoComplete="off"
              spellCheck={false}
              value={space}
              onChange={(e) => setSpace(e.target.value)}
            />
            <button className="btn press" type="submit" disabled={!validSpaceId(space)}>
              Install
            </button>
          </form>
          {privateJob ? (
            <small className="job-line">
              {privateJob[0]}: <JobText job={privateJob[1]} />
            </small>
          ) : null}
        </>
      ) : (
        <form className="inline-form" onSubmit={submitToken}>
          <input
            className="field"
            type="password"
            placeholder="Access token (hf_…)"
            aria-label="Hugging Face access token"
            autoComplete="new-password"
            spellCheck={false}
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
          <button className="btn press" type="submit" disabled={catalog.hfBusy || !tokenShape(token)}>
            {catalog.hfBusy ? <span className="spin" aria-hidden="true"></span> : null}
            Sign in
          </button>
        </form>
      )}
      <p className={`note${catalog.hfError ? " error" : ""}`}>
        {catalog.hfError ??
          (hf?.logged_in
            ? ""
            : "Only needed for private Spaces. Create a read token at huggingface.co/settings/tokens; Reachy keeps it, this page never shows it again.")}
      </p>
    </div>
  );
}

interface Props {
  catalog: CatalogState;
  /** The daemon's app API answers (the robot may be off). */
  reachable: boolean;
}

/** Apps to install from Hugging Face Spaces, as compact rows with a search (#126). */
export function CatalogPanel({ catalog, reachable }: Props) {
  const sheet = useLens<HTMLElement>();
  const [query, setQuery] = useState("");
  const shown = filterCatalog(catalog.apps, query);

  return (
    <section className="glass sheet catalog-sheet" data-lens ref={sheet} aria-labelledby="catalog-title">
      <header>
        <h2 id="catalog-title">Get apps</h2>
        <Segmented
          label="Catalog"
          options={[
            { value: "featured", label: "Featured" },
            { value: "all", label: "All Spaces" },
          ]}
          value={catalog.all ? "all" : "featured"}
          onChange={(v) => catalog.setAll(v === "all")}
        />
      </header>
      <input
        className="field search"
        type="search"
        placeholder="Search apps"
        aria-label="Search apps"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {!reachable ? (
        <p className="note">The catalog shows up when the daemon is online.</p>
      ) : catalog.error ? (
        <p className="note error">{catalog.error}</p>
      ) : catalog.loading && !catalog.apps.length ? (
        <p className="note">
          <span className="spin" aria-hidden="true"></span> Loading apps from Hugging Face…
        </p>
      ) : shown.length ? (
        <ul className="app-list" aria-label="Apps to install">
          {shown.map((app) => (
            <CatalogRow key={app.id} app={app} job={catalog.jobs[app.name]} onInstall={() => catalog.install(app)} />
          ))}
        </ul>
      ) : (
        <p className="note">{query ? "No apps match that search." : "No apps found."}</p>
      )}
      <HfLoginRow catalog={catalog} />
    </section>
  );
}
