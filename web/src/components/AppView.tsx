import type { AppView as AppViewState } from "../hooks/useApps.ts";
import "./AppView.css";

/**
 * A running app's own UI inside the dashboard, for phones and tablets where
 * tabs are hidden and a home-screen web app has no back button (#159). The bar
 * stays on top: "Controller" closes the view (the app keeps running), "Stop"
 * stops the app. Over HTTPS the frame loads the app through the controller's
 * proxy on :8043 (#158), so it's a secure context like the dashboard.
 */
export function AppView({
  view,
  onClose,
  onStop,
}: {
  view: AppViewState;
  onClose: () => void;
  onStop: () => void;
}) {
  return (
    <div className="app-view" role="dialog" aria-label={`${view.display} interface`}>
      <div className="app-view-bar">
        <button type="button" onClick={onClose}>
          ← Controller
        </button>
        <span className="app-view-title">{view.display}</span>
        <button type="button" className="app-view-stop" onClick={onStop}>
          Stop
        </button>
      </div>
      <iframe
        className="app-view-frame"
        src={view.url}
        title={view.display}
        allow="microphone; camera; autoplay; fullscreen; clipboard-read; clipboard-write; display-capture"
      />
    </div>
  );
}
