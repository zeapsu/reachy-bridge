import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson, paths, type Kinematics, type RobotState } from "./api.ts";
import { AppsPanel } from "./components/AppsPanel.tsx";
import { CatalogPanel } from "./components/CatalogPanel.tsx";
import { AudioPanel } from "./components/AudioPanel.tsx";
import { ControlsPanel, type ControlHint } from "./components/ControlsPanel.tsx";
import { ControlBar } from "./components/ControlBar.tsx";
import { ControlPanel } from "./components/ControlPanel.tsx";
import { DevicesPanel } from "./components/DevicesPanel.tsx";
import { DrivePanel } from "./components/DrivePanel.tsx";
import { ExpressionsPanel } from "./components/ExpressionsPanel.tsx";
import { AppearancePanel, Wallpaper } from "./components/Appearance.tsx";
import { AuthGate } from "./components/Login.tsx";
import { Segmented } from "./components/Segmented.tsx";
import { PhoneTabs, TopBar } from "./components/Shell.tsx";
import { SimPanel } from "./components/SimPanel.tsx";
import { VoicePanel } from "./components/VoicePanel.tsx";
import { LogsPanel, StatsPanel } from "./components/SystemPanel.tsx";
import { Viewport, type PoseSink } from "./components/Viewport.tsx";
import { useLens } from "./glass.ts";
import { useAppearance } from "./hooks/useAppearance.ts";
import { useApps } from "./hooks/useApps.ts";
import { useCatalog } from "./hooks/useCatalog.ts";
import { useDrive } from "./hooks/useDrive.ts";
import { AppView } from "./components/AppView.tsx";
import { useControl } from "./hooks/useControl.ts";
import { readEventPayload, useEventSource } from "./hooks/useEventSource.ts";
import { useExpressions } from "./hooks/useExpressions.ts";
import { usePageVisible } from "./hooks/usePageVisible.ts";
import { usePoll } from "./hooks/usePoll.ts";
import { usePower } from "./hooks/usePower.ts";
import { useSim } from "./hooks/useSim.ts";
import { useSimSounds } from "./hooks/useSimSounds.ts";
import { useVoice } from "./hooks/useVoice.ts";
import { activeApp } from "./logic/apps.ts";
import { read, write } from "./logic/appearance.ts";
import { controlState } from "./logic/control.ts";
import { CONTROL_HINT_OFFLINE } from "./logic/controls.ts";
import { driveBlocked } from "./logic/drive.ts";
import { TAB_KEY, parseTab, stageNote, type Tab } from "./logic/shell.ts";
import { voiceBadge } from "./logic/voice.ts";

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
  const [pane, setPane] = useState<"expressions" | "drive" | "controls">("expressions");
  const side = useLens<HTMLElement>();

  const reloadKinematics = useCallback(async () => {
    try {
      setKinematics(await apiJson<Kinematics>(paths.kinematics));
    } catch {
      setKinematics(null);
    }
  }, []);

  const control = useControl(visible);
  const voice = useVoice(visible);
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
  // Installs, updates and the catalog go through the daemon's app API, which
  // works with the robot off (the state stream, daemonOnline, needs it on).
  const appsReachable = daemonOnline || power.reachable;

  // The power, apps and kinematics shown are the new robot's after a switch.
  const sim = useSim(power.target, () => {
    // An app view open across a switch shows the other robot's app.
    apps.closeAppView();
    void power.poll();
    void apps.poll();
    void reloadKinematics();
  });
  // The sim has no speaker: its sounds play here instead (#219).
  const simSounds = useSimSounds(visible && sim.target === "sim" && !sim.switching);
  // The catalog marks what the current target has installed: wait for it.
  const catalog = useCatalog(visible && tab === "apps" && appsReachable && sim.target !== null, () => void apps.poll());

  const blocked = driveBlocked({
    daemonOnline,
    motorMode: robotControlMode,
    appActive,
    exprBusy: expr.busy !== null,
    controlBusy,
  });
  // Leaving the pane, the tab or the page ends a drive session.
  const drive = useDrive(blocked === null && visible && tab === "robot" && pane === "drive");

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
          sim={sim.target === "sim"}
          voice={voiceBadge(voice.view)}
        />

        <main className={`view robot${pane === "drive" ? " driving" : ""}`} id="view-robot" role="tabpanel" hidden={tab !== "robot"} aria-label="Robot">
          <Viewport daemonOnline={daemonOnline} note={stageNote(power.view, daemonOnline)} visible={visible && tab === "robot"} poseSink={poseSink} />
          <section className="glass sheet side" data-lens ref={side} aria-label="Robot controls">
            <ControlBar control={control} />
            <VoicePanel voice={voice} control={controlState(control.view)} />
            <Segmented
              label="Robot controls"
              options={[
                { value: "expressions", label: "Expressions" },
                { value: "drive", label: "Drive" },
                { value: "controls", label: "Controls" },
              ]}
              value={pane}
              onChange={setPane}
            />
            <div hidden={pane !== "expressions"}>
              <ExpressionsPanel expr={expr} daemonOnline={daemonOnline} controlBusy={controlBusy} appActive={appActive} />
            </div>
            <div hidden={pane !== "drive"}>
              <DrivePanel drive={drive} blocked={blocked} />
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

        <main className="view list" id="view-apps" role="tabpanel" hidden={tab !== "apps"} aria-label="Apps">
          {/* Nothing until the target is known: the robot's and the sim's apps differ (#218). */}
          {sim.target === null ? null : (
            <>
              <AppsPanel apps={apps} catalog={catalog} daemonOnline={daemonOnline} reachable={appsReachable} exprBusy={expr.busy !== null} />
              <CatalogPanel catalog={catalog} reachable={appsReachable} />
            </>
          )}
        </main>

        <main className="view list" id="view-system" role="tabpanel" hidden={tab !== "system"} aria-label="System">
          <StatsPanel daemonOnline={daemonOnline} live={visible && tab === "system"} />
          <AudioPanel daemonOnline={daemonOnline} live={visible && tab === "system"} />
          <SimPanel sim={sim} sounds={simSounds} powerStatus={power.status} />
          <LogsPanel live={visible && tab === "system"} />
          <ControlPanel control={control} />
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
