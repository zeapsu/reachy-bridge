import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson, errorText, paths, type Kinematics, type RobotState } from "./api.ts";
import { AppsPanel } from "./components/AppsPanel.tsx";
import { CatalogPanel } from "./components/CatalogPanel.tsx";
import { ControlsPanel, type ControlHint } from "./components/ControlsPanel.tsx";
import { ControlStrip, ControlToast } from "./components/ControlStrip.tsx";
import { DevicesPanel } from "./components/DevicesPanel.tsx";
import { DrivePanel } from "./components/DrivePanel.tsx";
import { ExpressionsPanel } from "./components/ExpressionsPanel.tsx";
import { AppearancePanel, Wallpaper } from "./components/Appearance.tsx";
import { AuthGate } from "./components/Login.tsx";
import { Segmented } from "./components/Segmented.tsx";
import { PhoneTabs, TopBar } from "./components/Shell.tsx";
import { ProfilePicker, ProfileSheet } from "./components/Profiles.tsx";
import { SystemView } from "./components/SystemView.tsx";
import { VoicePanel } from "./components/VoicePanel.tsx";
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
import { useProfile } from "./hooks/useProfile.ts";
import { useSim } from "./hooks/useSim.ts";
import { useSimSounds } from "./hooks/useSimSounds.ts";
import { useVoice } from "./hooks/useVoice.ts";
import { activeApp, appForName } from "./logic/apps.ts";
import { read, write } from "./logic/appearance.ts";
import { controlState, holderLabel } from "./logic/control.ts";
import { driveBlocked } from "./logic/drive.ts";
import { TAB_KEY, parseTab, stageNote, type Tab } from "./logic/shell.ts";
import { voiceBadge, voiceLive } from "./logic/voice.ts";
import { allowedApps } from "./logic/profile.ts";

const POLL_MS = 1000;

type Pane = "expressions" | "drive" | "controls" | "voice";
const PANES: Pane[] = ["expressions", "drive", "controls", "voice"];

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
  const [controlHint, setControlHint] = useState<ControlHint>({ text: "", error: false });
  const [robotControlMode, setRobotControlMode] = useState<string | null>(null);
  const [kinematics, setKinematics] = useState<Kinematics | null>(null);
  const poseSink = useRef<PoseSink | null>(null);
  const look = useAppearance();
  const [savedTab, setTabState] = useState<Tab>(() => parseTab(read(TAB_KEY)));
  const [paneChoice, setPane] = useState<Pane>("expressions");
  const me = useProfile(visible);
  const may = me.rights;
  const [sheetOpen, setSheetOpen] = useState(false);
  // Profiles (#297): what this profile can't open is dimmed, and never shown.
  const paneOff: Record<Pane, boolean> = {
    expressions: !may.can.drive,
    drive: !may.can.drive,
    controls: !may.controls,
    voice: !may.can.voice,
  };
  const pane: Pane = paneOff[paneChoice] ? (PANES.find((p) => !paneOff[p]) ?? paneChoice) : paneChoice;
  const systemOpen = me.loaded && may.system;
  const tab: Tab = savedTab === "system" && !systemOpen ? "robot" : savedTab;
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
  const [driveFixError, setDriveFixError] = useState<string | null>(null);
  useEffect(() => setDriveFixError(null), [robotControlMode]);
  const enableMotors = async () => {
    setControlBusy(true);
    setDriveFixError(null);
    try {
      await apiJson(paths.motorSetMode("enabled"), { method: "POST" });
      setRobotControlMode("enabled");
      // The backend kicks a return-home session (#67).
      expr.enterReturningHome();
    } catch (err) {
      setDriveFixError(errorText(err, "Couldn't enable motors"));
    } finally {
      setControlBusy(false);
    }
  };
  // Leaving the pane, the tab or the page ends a drive session.
  const drive = useDrive(blocked === null && visible && tab === "robot" && pane === "drive");

  usePoll(power.poll, POLL_MS, visible);
  usePoll(apps.poll, POLL_MS, visible);
  useEffect(() => {
    if (visible) void apps.loadFavorites();
  }, [visible, apps.loadFavorites]);
  // Pins are each person's own: a tablet that switches profile reloads them.
  const actingPerson = me.profile ? (me.profile.name ?? "") : null;
  const pinsOwner = useRef(actingPerson);
  useEffect(() => {
    if (pinsOwner.current === actingPerson) return;
    const first = pinsOwner.current === null;
    pinsOwner.current = actingPerson;
    if (!first) void apps.reloadFavorites();
  }, [actingPerson, apps.reloadFavorites]);

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
          voice={voiceBadge(voice.view, voice.mine)}
          voiceLive={voiceLive(voice.view, voice.mine)}
          holder={holderLabel(controlState(control.view))}
          onRelease={() => void control.act(paths.controlRelease)}
          controlBusy={control.busy}
          profile={me.profile}
          onProfile={() => setSheetOpen(true)}
          systemLocked={me.loaded && !may.system}
          powerLocked={!may.can.power}
        />

        <main className={`view robot${pane === "drive" ? " driving" : ""}`} id="view-robot" role="tabpanel" hidden={tab !== "robot"} aria-label="Robot">
          <Viewport
            daemonOnline={daemonOnline}
            note={stageNote(power.view, daemonOnline)}
            onPower={power.toggle}
            powerDisabled={power.view.disabled || !may.can.power}
            cameraLocked={!may.can.camera}
            visible={visible && tab === "robot"}
            poseSink={poseSink}
          />
          <section className="glass sheet side" data-lens ref={side} aria-label="Robot controls">
            <ControlStrip control={control} />
            <Segmented
              label="Robot controls"
              options={[
                { value: "expressions", label: "Expressions", disabled: paneOff.expressions },
                { value: "drive", label: "Drive", disabled: paneOff.drive },
                { value: "controls", label: "Controls", disabled: paneOff.controls },
                { value: "voice", label: "Voice", disabled: paneOff.voice },
              ]}
              value={pane}
              onChange={setPane}
            />
            <div hidden={pane !== "expressions" || paneOff.expressions}>
              <ExpressionsPanel expr={expr} daemonOnline={daemonOnline} controlBusy={controlBusy} appActive={appActive} />
            </div>
            <div hidden={pane !== "drive" || paneOff.drive}>
              <DrivePanel
                drive={drive}
                blocked={blocked}
                actionBusy={controlBusy || apps.appBusy}
                fixError={blocked === "motors" ? driveFixError : blocked === "app" && apps.actionError?.name === active?.name ? (apps.actionError?.text ?? null) : null}
                onEnableMotors={() => void enableMotors()}
                onStopApp={() => active && apps.perform(active.name, "stop")}
              />
            </div>
            <div hidden={pane !== "controls" || paneOff.controls}>
              <ControlsPanel
                daemonOnline={daemonOnline}
                controlBusy={controlBusy}
                setControlBusy={setControlBusy}
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
                motorsLocked={!may.can.drive}
              />
            </div>
            <div hidden={pane !== "voice" || paneOff.voice}>
              <VoicePanel voice={voice} control={controlState(control.view)} robotOff={power.view.known && (power.view.powerClass === "" || power.view.powerClass === "disabled")} />
            </div>
          </section>
        </main>

        <main className="view list" id="view-apps" role="tabpanel" hidden={tab !== "apps"} aria-label="Apps">
          {/* Nothing until the target is known: the robot's and the sim's apps differ (#218). */}
          {sim.target === null ? null : (
            <>
              <AppsPanel
                apps={apps}
                catalog={catalog}
                daemonOnline={daemonOnline}
                reachable={appsReachable}
                known={power.view.known}
                exprBusy={expr.busy !== null}
                names={allowedApps(apps.names, may.apps)}
                canManage={may.admin}
              />
              {/* Installing is the admin's (#221). */}
              {may.admin ? <CatalogPanel catalog={catalog} reachable={appsReachable} known={power.view.known} /> : null}
            </>
          )}
        </main>

        <main className="view list" id="view-system" role="tabpanel" hidden={tab !== "system"} aria-label="System">
          {/* System is the admin's (#297): its panels load only for one. */}
          {systemOpen ? (
            <SystemView
              family={may.family}
              person={me.profile?.name ?? null}
              installed={apps.names.map((name) => ({ name, display: appForName(apps.statuses, name).display || name }))}
              daemonOnline={daemonOnline}
              live={visible && tab === "system"}
              sim={sim}
              sounds={simSounds}
              powerStatus={power.status}
              control={control}
              look={look}
              onOpenApps={() => setTab("apps")}
            />
          ) : null}
        </main>
      </div>
      <PhoneTabs tab={tab} onTab={setTab} systemLocked={me.loaded && !may.system} />
      {me.profile && sheetOpen ? (
        <ProfileSheet
          profile={me.profile}
          onClose={() => setSheetOpen(false)}
          onSwitch={() => {
            setSheetOpen(false);
            void me.pick(null);
          }}
        >
          {me.profile.shared || me.profile.admin ? null : <DevicesPanel />}
          <AppearancePanel look={look} />
        </ProfileSheet>
      ) : null}
      {may.picking ? <ProfilePicker onPick={(person) => me.pick(person)} error={me.error} /> : null}
      <ControlToast control={control} />
      {apps.appView ? (
        <AppView
          view={apps.appView}
          busy={apps.appBusy || control.busy}
          onClose={apps.closeAppView}
          onStop={() => apps.perform(apps.appView!.name, "stop")}
          onTake={(() => {
            const state = controlState(control.view);
            return state.kind === "free" && state.canTake ? () => void control.act(paths.controlTakeIfFree) : undefined;
          })()}
        />
      ) : null}
    </>
  );
}
