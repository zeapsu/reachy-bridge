import { useState, type ReactNode } from "react";
import { useLens } from "../glass.ts";
import type { Appearance } from "../hooks/useAppearance.ts";
import type { ControlApi } from "../hooks/useControl.ts";
import type { SimState } from "../hooks/useSim.ts";
import type { SimSoundsState } from "../hooks/useSimSounds.ts";
import { useWaiting } from "../hooks/useWaiting.ts";
import { useWakeApp } from "../hooks/useWakeApp.ts";
import { read, write } from "../logic/appearance.ts";
import { SYSTEM_PANE_KEY, parseSystemPane, type SystemPane } from "../logic/shell.ts";
import { AccessPanel } from "./AccessPanel.tsx";
import { AppearanceRows } from "./Appearance.tsx";
import { DevicesPanel } from "./DevicesPanel.tsx";
import { Panel } from "./Panel.tsx";
import type { InstalledApp } from "./Profiles.tsx";
import { Segmented } from "./Segmented.tsx";
import { ROW_ICONS } from "./SettingsRow.tsx";
import { SimRows } from "./SimPanel.tsx";
import { Diagnostics } from "./SystemPanel.tsx";
import { WakeAppRow } from "./WakeAppRow.tsx";
import "./SystemView.css";

const MANUAL = "https://github.com/zeapsu/reachy-bridge/blob/main/docs/manual.md";

const PANES: { value: SystemPane; label: ReactNode }[] = [
  { value: "settings", label: "Settings" },
  { value: "access", label: "Access" },
  { value: "diagnostics", label: "Diagnostics" },
];

interface Props {
  /** Profiles apply (admin mode): Access lists them. */
  family: boolean;
  /** Who uses this browser, in admin mode. */
  person: string | null;
  installed: InstalledApp[];
  daemonOnline: boolean;
  /** The System tab is showing: its live streams may run. */
  live: boolean;
  sim: SimState;
  sounds: SimSoundsState;
  powerStatus: string | null;
  control: ControlApi;
  look: Appearance;
  /** Open the Apps tab, where a missing app is installed. */
  onOpenApps: () => void;
}

/**
 * System (#334): one sheet with a section per tab, as the Robot tab's side
 * sheet. Settings changes things, Access says who may use the robot, and
 * Diagnostics shows how the board is doing.
 */
export function SystemView({ family, person, installed, daemonOnline, live, sim, sounds, powerStatus, control, look, onOpenApps }: Props) {
  const sheet = useLens<HTMLElement>();
  const [pane, setPaneState] = useState<SystemPane>(() => parseSystemPane(read(SYSTEM_PANE_KEY)));
  const setPane = (next: SystemPane) => {
    setPaneState(next);
    write(SYSTEM_PANE_KEY, next);
  };
  const waiting = useWaiting(live && family);
  const panes = PANES.map((p) =>
    p.value === "access" && waiting.length > 0
      ? {
          ...p,
          label: (
            <>
              Access
              <span className="seg-badge" role="img" aria-label="a device is waiting"></span>
            </>
          ),
        }
      : p,
  );
  const wake = useWakeApp(live && pane === "settings", sim.target, daemonOnline);
  return (
    <section className="glass sheet sys" data-lens ref={sheet} aria-label="System">
      <Segmented role="tabs" label="System sections" options={panes} value={pane} onChange={setPane} panelId={(p) => `system-${p}`} />
      <div className="pane" id="system-settings" role="tabpanel" aria-label="Settings" hidden={pane !== "settings"}>
        <Panel inset title="Robot" titleId="settings-robot-title">
          <SimRows sim={sim} sounds={sounds} powerStatus={powerStatus} control={control}>
            <WakeAppRow wake={wake} onInstall={onOpenApps} />
          </SimRows>
          <p className="note error" role="alert">
            {wake.error}
          </p>
        </Panel>
        <Panel inset title="Appearance" titleId="settings-look-title">
          <AppearanceRows look={look} />
        </Panel>
        <a className="set-row manual-link" href={MANUAL} target="_blank" rel="noopener noreferrer">
          <span className="set-ico">{ROW_ICONS.manual}</span>
          <span className="set-label">User manual</span>
          <span className="set-trail">
            <span className="out" aria-hidden="true">
              ↗
            </span>
          </span>
        </a>
      </div>
      <div className="pane" id="system-access" role="tabpanel" aria-label="Access" hidden={pane !== "access"}>
        {family ? <AccessPanel installed={installed} person={person} waiting={waiting} live={live && pane === "access"} holder={control.view?.holder ?? null} /> : <DevicesPanel inset />}
      </div>
      <div className="pane" id="system-diagnostics" role="tabpanel" aria-label="Diagnostics" hidden={pane !== "diagnostics"}>
        <Diagnostics daemonOnline={daemonOnline} live={live && pane === "diagnostics"} />
      </div>
    </section>
  );
}
