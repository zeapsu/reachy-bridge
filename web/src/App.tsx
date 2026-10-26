import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson, paths, type Kinematics, type RobotState } from "./api.ts";
import { AppsPanel } from "./components/AppsPanel.tsx";
import { AudioPanel } from "./components/AudioPanel.tsx";
import { ControlsPanel, type ControlHint } from "./components/ControlsPanel.tsx";
import { DevicesPanel } from "./components/DevicesPanel.tsx";
import { ExpressionsPanel } from "./components/ExpressionsPanel.tsx";
import { AppearancePanel, Wallpaper } from "./components/Appearance.tsx";
import { AuthGate } from "./components/Login.tsx";
import { Segmented } from "./components/Segmented.tsx";
import { PhoneTabs, TopBar } from "./components/Shell.tsx";
import { LogsPanel, StatsPanel } from "./components/SystemPanel.tsx";
import { Viewport, type PoseSink } from "./components/Viewport.tsx";
import { useLens } from "./glass.ts";
import { useAppearance } from "./hooks/useAppearance.ts";
import { useApps } from "./hooks/useApps.ts";
import { AppView } from "./components/AppView.tsx";
import { readEventPayload, useEventSource } from "./hooks/useEventSource.ts";
import { useExpressions } from "./hooks/useExpressions.ts";
import { usePageVisible } from "./hooks/usePageVisible.ts";
import { usePoll } from "./hooks/usePoll.ts";
import { usePower } from "./hooks/usePower.ts";
import { activeApp } from "./logic/apps.ts";
import { read, write } from "./logic/appearance.ts";
import { CONTROL_HINT_OFFLINE } from "./logic/controls.ts";
import { TAB_KEY, parseTab, type Tab } from "./logic/shell.ts";

const POLL_MS = 1000;

/** The login screen when LAN access needs it (#141), else the dashboard. */
export function App() {
  return (
    <AuthGate>
      <Dashboard />
    </AuthGate>
  );
}

/**
 * Dashboard root. Owns the cross-panel state main.js kept in one object:
 * daemon online (telemetry stream), the active app, control busy/status,
 * motor mode, kinematics, and the expression session. Live connections run
 * only while the tab is visible.
 */
function Dashboard() {
  const visible = usePageVisible();
  const [daemonOnline, setDaemonOnline] = useState(false);
  const [controlBusy, setControlBusy] = useState(false);
  const [controlStatus, setControlStatus] = useState("offline");
  const [controlHint, setControlHint] = useState<ControlHint>({ text: CONTROL_HINT_OFFLINE, error: false });
  const [robotControlMode, setRobotControlMode] = useState<string | null>(null);
  const [kinematics, setKinematics] = useState<Kinematics | null>(null);
  const poseSink = useRef<PoseSink | null>(null);
  const look = useAppearance();
  const [tab, setTabState] = useState<Tab>(() => parseTab(read(TAB_KEY)));
  const [pane, setPane] = useState<"expressions" | "controls">("expressions");
  const side = useLens<HTMLElement>();

  const reloadKinematics = useCallback(async () => {
    try {
      setKinematics(await apiJson<Kinematics>(paths.kinematics));
    } catch {
      setKinematics(null);
    }
  }, []);

  const apps = useApps();
  const active = activeApp(apps.statuses);
  const appActive = Boolean(active);
  const expr = useExpressions(daemonOnline, appActive);

  const power = usePower(
    {
      setControlBusy,
      setControlStatus,
      setControlHint: (text, error = false) => setControlHint({ text, error }),
    },
    // The running engine changes with power, so refresh the kinematics panel.
    () => void reloadKinematics(),
  );

  usePoll(power.poll, POLL_MS, visible);
  usePoll(apps.poll, POLL_MS, visible);
  useEffect(() => {
    if (visible) void apps.loadFavorites();
  }, [visible, apps.loadFavorites]);

  const setTab = (next: Tab) => {
    setTabState(next);
    write(TAB_KEY, next);
    // Retry a failed initial favorites fetch when the list comes into view.
    if (next === "apps" && !apps.favoritesLoaded()) void apps.loadFavorites();
  };

  useEventSource(visible ? paths.stateStream : null, {
    "state-update": (event) => {
      const payload = readEventPayload<RobotState>(event);
      setDaemonOnline(true);
      if (!payload || typeof payload !== "object") return;
      poseSink.current?.(payload);
      if ("control_mode" in payload) {
        setRobotControlMode(typeof payload.control_mode === "string" ? payload.control_mode : null);
      }
    },
    "daemon-offline": () => setDaemonOnline(false),
    error: () => setDaemonOnline(false),
  });

  return (
    <>
      <Wallpaper look={look} />
      <div className="app">
        <TopBar
          tab={tab}
          onTab={setTab}
          power={power.view}
          onPower={power.toggle}
          daemonOnline={daemonOnline}
          activeApp={active ? active.display || active.name : null}
        />

        <main className="view robot" hidden={tab !== "robot"} aria-label="Robot">
          <Viewport daemonOnline={daemonOnline} visible={visible && tab === "robot"} poseSink={poseSink} />
          <section className="glass sheet side" data-lens ref={side} aria-label="Robot controls">
            <Segmented
              label="Robot controls"
              options={[
                { value: "expressions", label: "Expressions" },
                { value: "controls", label: "Controls" },
              ]}
              value={pane}
              onChange={setPane}
            />
            <div hidden={pane !== "expressions"}>
              <ExpressionsPanel expr={expr} daemonOnline={daemonOnline} controlBusy={controlBusy} appActive={appActive} />
            </div>
            <div hidden={pane !== "controls"}>
              <ControlsPanel
                daemonOnline={daemonOnline}
                controlBusy={controlBusy}
                setControlBusy={setControlBusy}
                status={controlStatus}
                setStatus={setControlStatus}
                hint={controlHint}
                setHint={setControlHint}
                robotControlMode={robotControlMode}
                setRobotControlMode={setRobotControlMode}
                kinematics={kinematics}
                setKinematics={setKinematics}
                reloadKinematics={reloadKinematics}
                appActive={appActive}
                setStopping={power.setStopping}
                pollPower={power.poll}
                onReturningHome={expr.enterReturningHome}
              />
            </div>
          </section>
        </main>

        <main className="view list" hidden={tab !== "apps"} aria-label="Apps">
          <AppsPanel apps={apps} daemonOnline={daemonOnline} exprBusy={expr.busy !== null} />
        </main>

        <main className="view list" hidden={tab !== "system"} aria-label="System">
          <StatsPanel daemonOnline={daemonOnline} live={visible && tab === "system"} />
          <AudioPanel daemonOnline={daemonOnline} live={visible && tab === "system"} />
          <LogsPanel live={visible && tab === "system"} />
          <DevicesPanel />
          <AppearancePanel look={look} />
        </main>
      </div>
      <PhoneTabs tab={tab} onTab={setTab} />
      {apps.appView ? (
        <AppView
          view={apps.appView}
          busy={apps.appBusy}
          onClose={apps.closeAppView}
          onStop={() => apps.perform(apps.appView!.name, "stop")}
        />
      ) : null}
    </>
  );
}
