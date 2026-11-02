import type { ReactNode } from "react";
import { useLens } from "../glass.ts";
import type { PowerView } from "../logic/power.ts";
import { robotStatus, type Tab } from "../logic/shell.ts";
import type { VoiceRoute } from "../logic/voice.ts";
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

const stroke = { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true } as const;

const ROUTE_ICONS: Record<VoiceRoute, ReactNode> = {
  // An ear.
  listen: (
    <svg {...stroke}>
      <path d="M6 9a6 6 0 1 1 12 0c0 3-2 4-3 5.5S14 18 12 19.5 8 20 8 18" />
      <path d="M10 10a2 2 0 1 1 4 0" />
    </svg>
  ),
  // A speaker.
  hear: (
    <svg {...stroke}>
      <path d="M4 10v4h3l5 4V6L7 10H4Z" />
      <path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11" />
    </svg>
  ),
  // A microphone.
  talk: (
    <svg {...stroke}>
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
    </svg>
  ),
  // A speech bubble: talking to its apps.
  apps: (
    <svg {...stroke}>
      <path d="M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H10l-5 4v-4a2 2 0 0 1-1-2V6Z" />
    </svg>
  ),
};

const PERSON_ICON = (
  <svg {...stroke}>
    <circle cx="12" cy="8" r="4" />
    <path d="M4 21a8 8 0 0 1 16 0" />
  </svg>
);

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
  /** Who that is and which routes are live. */
  voiceLive: { who: string; routes: VoiceRoute[] } | null;
  /** Who is in control (#221): "you" or a person; null outside admin mode or when nobody is. */
  holder: string | null;
  /** Let go of control: shown while this browser has it. */
  onRelease: () => void;
  /** A control action is running. */
  controlBusy: boolean;
}

/** Brand, the section tabs (a bottom bar on phones), robot status and power. */
export function TopBar({ tab, onTab, power, onPower, daemonOnline, activeApp, sim, voice, voiceLive, holder, onRelease, controlBusy }: Props) {
  const tabs = useLens<HTMLDivElement>();
  const status = robotStatus(power, daemonOnline, activeApp);
  const person = holder && holder !== "you" ? holder : null;
  // On the running robot the person replaces On or the app; the power button already says on.
  const personOnly = person !== null && power.powerClass === "running" && !power.label;
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
            {voiceLive?.routes.length ? voiceLive.routes.map((route) => <span key={route}>{ROUTE_ICONS[route]}</span>) : <span>{ROUTE_ICONS.talk}</span>}
            <span className="voice-badge-text">{voiceLive?.who}</span>
          </span>
        ) : null}
        <span className="glass status" role="status">
          <span className={`dot ${status.dot}`}></span>
          {sim ? <span className="sim-badge">Sim</span> : null}
          <span className="status-text">
            {/* Release beside the pill says it's you. */}
            {personOnly ? (
              <span className="control-who person" aria-label={`${person} is in control`}>
                {PERSON_ICON}
                {person}
              </span>
            ) : (
              <>
                {status.text}
                {person ? <span className="control-who"> · {person}</span> : null}
              </>
            )}
          </span>
        </span>
        {holder === "you" ? (
          <button className="glass release press" type="button" disabled={controlBusy} onClick={onRelease}>
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

