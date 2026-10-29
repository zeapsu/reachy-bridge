import type { AppView as AppViewState } from "../hooks/useApps.ts";
import { LOADING_PROGRESS, LOADING_STEPS } from "../logic/loading.ts";
import "./AppView.css";

/**
 * A launched app's own UI inside the dashboard, on every device (#159): no
 * stray tabs, and a home-screen web app (no back button) gets a one-tap way
 * back. Until the app's server answers it shows the start steps. The bar
 * stays on top: "Controller" closes the view (the app keeps running), "Stop"
 * stops the app; the view also closes by itself when the app stops or
 * crashes. The frame loads the app through the controller's proxy: :8043 over
 * HTTPS (#158), a secure context like the dashboard, or :7863 over plain http,
 * behind the LAN login (#186).
 */
export function AppView({
  view,
  busy,
  onClose,
  onStop,
  onTake,
}: {
  view: AppViewState;
  busy: boolean;
  onClose: () => void;
  onStop: () => void;
  /** Nobody is in control and this browser may take it (#221): the app's own UI can't take it for you. */
  onTake?: () => void;
}) {
  return (
    <div className="app-view" role="dialog" aria-label={`${view.display} interface`}>
      <div className="app-view-panel">
        <div className="app-view-bar">
          <button type="button" className="app-view-back" onClick={onClose}>
            ← Controller
          </button>
          <span className="app-view-title">{view.display}</span>
          {view.url ? (
            <a className="app-view-tab" href={view.url} target="_blank" rel="noopener" title="Open in a new tab">
              ↗
            </a>
          ) : null}
          {onTake ? (
            <button type="button" className="app-view-stop" onClick={onTake} disabled={busy}>
              Take control
            </button>
          ) : null}
          <button type="button" className="app-view-stop" onClick={onStop} disabled={busy}>
            Stop
          </button>
        </div>
        {view.url && !view.error ? (
          <iframe
            className="app-view-frame"
            src={view.url}
            title={view.display}
            allow="microphone; camera; autoplay; fullscreen; clipboard-read; clipboard-write; display-capture"
          />
        ) : (
          <Starting view={view} />
        )}
      </div>
    </div>
  );
}

const STEP_LABELS = { waking: "Waking Reachy", starting: "Starting the app", opening: "Opening its interface" };

function Starting({ view }: { view: AppViewState }) {
  const at = LOADING_STEPS.indexOf(view.step);
  return (
    <div className="app-view-body">
      <section className={`app-view-card${view.error ? " failed" : ""}`} aria-live="polite">
        <h2>{view.error ? `${view.display} didn't start` : `Starting ${view.display}`}</h2>
        <ol className="app-view-steps">
          {LOADING_STEPS.map((step, index) => (
            <li key={step} className={view.error ? "" : index < at ? "done" : index === at ? "active" : ""}>
              <span className="mark" />
              {STEP_LABELS[step]}
            </li>
          ))}
        </ol>
        <div className="app-view-progress">
          <span style={{ width: `${view.error ? 100 : LOADING_PROGRESS[view.step]}%` }} />
        </div>
        <p>{view.error || "The app opens here when it's ready."}</p>
      </section>
    </div>
  );
}
