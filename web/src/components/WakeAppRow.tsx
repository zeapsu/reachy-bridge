import type { CSSProperties } from "react";
import type { WakeAppState } from "../hooks/useWakeApp.ts";
import { appHue, appInitial, wakeLabel } from "../logic/apps.ts";
import { ROW_ICONS, SettingsRow } from "./SettingsRow.tsx";

/**
 * Conversation on wake (#334): the app starts each time the dashboard powers
 * the robot on. The warning stays while it is on: the robot moves and
 * listens without anyone starting an app.
 */
export function WakeAppRow({ wake, onInstall }: { wake: WakeAppState; onInstall: () => void }) {
  const { info } = wake;
  const name = info?.name ?? "Conversation App";
  const missing = info !== null && !info.installed;
  const on = Boolean(info?.installed && info.enabled);
  const tile = (
    <span className="app-tile" style={{ "--hue": appHue(info?.app ?? "") } as CSSProperties} aria-hidden="true">
      {appInitial(name)}
    </span>
  );
  const sub = missing ? (
    "Not installed"
  ) : on ? (
    <>
      {ROW_ICONS.warn}
      Reachy moves and listens as soon as it wakes
    </>
  ) : undefined;
  return (
    <SettingsRow icon={tile} label={wakeLabel(name)} labelId="wake-app-label" sub={sub} subId="wake-app-hint" warn={on} off={!info?.installed}>
      {missing ? (
        <button className="btn press" type="button" onClick={onInstall}>
          Install
        </button>
      ) : (
        <>
          {wake.busy ? <span className="spin" aria-hidden="true"></span> : null}
          <button
            type="button"
            className="switch"
            role="switch"
            id="wake-app"
            aria-labelledby="wake-app-label"
            aria-describedby={sub ? "wake-app-hint" : undefined}
            aria-checked={on}
            disabled={!info || wake.busy}
            onClick={() => wake.set(!on)}
          ></button>
        </>
      )}
    </SettingsRow>
  );
}
