import type { ReactNode } from "react";
import { useLens } from "../glass.ts";
import type { PowerView } from "../logic/power.ts";
import { robotStatus, type Tab } from "../logic/shell.ts";
import { Segmented } from "./Segmented.tsx";
import "./Shell.css";

const ICONS: Record<Tab, ReactNode> = {
  robot: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="5" y="7" width="14" height="10" rx="4" />
      <circle cx="9.5" cy="12" r="1.6" />
      <circle cx="14.5" cy="12" r="1.6" />
      <path d="M8 7 6.5 3M16 7l1.5-4" />
    </svg>
  ),
  apps: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" aria-hidden="true">
      <rect x="4" y="4" width="7" height="7" rx="2" />
      <rect x="13" y="4" width="7" height="7" rx="2" />
      <rect x="4" y="13" width="7" height="7" rx="2" />
      <rect x="13" y="13" width="7" height="7" rx="2" />
    </svg>
  ),
  system: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
      <path d="M4 18V9M10 18V5M16 18v-6M22 18H2" />
    </svg>
  ),
};

const LABELS: Record<Tab, string> = { robot: "Robot", apps: "Apps", system: "System" };
const ORDER: Tab[] = ["robot", "apps", "system"];

interface Props {
  tab: Tab;
  onTab: (tab: Tab) => void;
  power: PowerView;
  onPower: () => void;
  daemonOnline: boolean;
  activeApp: string | null;
  /** The controller drives the simulated robot (#149). */
  sim: boolean;
  /** Whose mic is live, when a listen or talk session is open (#219). */
  voice: string | null;
  /** Who is in control (#221): "you" or a person; null outside admin mode or when nobody is. */
  holder: string | null;
  /** Let go of control: shown while this browser has it. */
  onRelease: () => void;
  /** A control action is running. */
  controlBusy: boolean;
}

/** Brand, the section tabs (a bottom bar on phones), robot status and power. */
export function TopBar({ tab, onTab, power, onPower, daemonOnline, activeApp, sim, voice, holder, onRelease, controlBusy }: Props) {
  const tabs = useLens<HTMLDivElement>();
  const status = robotStatus(power, daemonOnline, activeApp);
  return (
    <header className="top">
      <div className="brand">reachy</div>
      <div className="glass tabs-glass" data-lens ref={tabs}>
        <Segmented
          role="tabs"
          label="Sections"
          className="tabs"
          options={ORDER.map((t) => ({ value: t, label: LABELS[t] }))}
          panelId={(t) => `view-${t}`}
          value={tab}
          onChange={onTab}
        />
      </div>
      <div className="top-right">
        {voice ? (
          <span className="glass voice-badge" role="status" title={voice} aria-label={voice}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <rect x="9" y="3" width="6" height="11" rx="3" />
              <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
            </svg>
            <span className="voice-badge-text">{voice}</span>
          </span>
        ) : null}
        <span className="glass status" role="status">
          <span className={`dot ${status.dot}`}></span>
          {sim ? <span className="sim-badge">Sim</span> : null}
          <span className="status-text">
            {status.text}
            {/* Release beside the pill says it's you. */}
            {holder && holder !== "you" ? <span className="control-who"> · {holder}</span> : null}
          </span>
        </span>
        {holder === "you" ? (
          <button className="glass release press" type="button" title="Let go of control, so others can drive" disabled={controlBusy} onClick={onRelease}>
            Release
          </button>
        ) : null}
        <button
          className={`glass power press ${power.powerClass}`}
          type="button"
          id="daemon-power"
          title={power.title}
          aria-label={power.title}
          disabled={power.disabled}
          onClick={onPower}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" aria-hidden="true">
            <path d="M12 3v8" />
            <path d="M6.3 7.2a8 8 0 1 0 11.4 0" />
          </svg>
        </button>
      </div>
    </header>
  );
}

export function PhoneTabs({ tab, onTab }: { tab: Tab; onTab: (tab: Tab) => void }) {
  const bar = useLens<HTMLElement>();
  return (
    <nav className="glass phone-tabs" data-lens ref={bar}>
      <Segmented
        role="tabs"
        label="Sections"
        className="tabs"
        options={ORDER.map((t) => ({
          value: t,
          label: (
            <>
              {ICONS[t]}
              <span>{LABELS[t]}</span>
            </>
          ),
        }))}
        panelId={(t) => `view-${t}`}
        value={tab}
        onChange={onTab}
      />
    </nav>
  );
}

