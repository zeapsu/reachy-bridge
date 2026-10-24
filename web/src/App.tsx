import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson, paths, type Kinematics, type RobotState } from "./api.ts";
import { AppsPanel } from "./components/AppsPanel.tsx";
import { AudioPanel } from "./components/AudioPanel.tsx";
import { ControlsPanel, type ControlHint } from "./components/ControlsPanel.tsx";
import { ExpressionsPanel } from "./components/ExpressionsPanel.tsx";
import { StatusBar } from "./components/StatusBar.tsx";
import { SystemPanel } from "./components/SystemPanel.tsx";
import { ThemePopover } from "./components/ThemePicker.tsx";
import { Viewport, type PoseSink } from "./components/Viewport.tsx";
import { useApps } from "./hooks/useApps.ts";
import { AppView } from "./components/AppView.tsx";
import { readEventPayload, useEventSource } from "./hooks/useEventSource.ts";
import { useExpressions } from "./hooks/useExpressions.ts";
import { usePageVisible } from "./hooks/usePageVisible.ts";
import { usePoll } from "./hooks/usePoll.ts";
import { usePower } from "./hooks/usePower.ts";
import { activeApp } from "./logic/apps.ts";
import { CONTROL_HINT_OFFLINE } from "./logic/controls.ts";

const POLL_MS = 1000;

/**
 * Dashboard root. Owns the cross-panel state main.js kept in one object:
 * daemon online (telemetry stream), the active app, control busy/status,
 * motor mode, kinematics, and the expression session. Live connections run
 * only while the tab is visible.
 */
export function App() {
  const visible = usePageVisible();
  const [daemonOnline, setDaemonOnline] = useState(false);
  const [controlBusy, setControlBusy] = useState(false);
  const [controlStatus, setControlStatus] = useState("offline");
  const [controlHint, setControlHint] = useState<ControlHint>({ text: CONTROL_HINT_OFFLINE, error: false });
  const [robotControlMode, setRobotControlMode] = useState<string | null>(null);
  const [kinematics, setKinematics] = useState<Kinematics | null>(null);
  const poseSink = useRef<PoseSink | null>(null);

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
      <ThemePopover />
      <div className="shell">
        <StatusBar daemonOnline={daemonOnline} activeApp={active?.name ?? null} />
        <div className="grid">
          <div className="col">
            <Viewport
              daemonOnline={daemonOnline}
              visible={visible}
              power={power.view}
              onPower={power.toggle}
              poseSink={poseSink}
            />
            <AudioPanel daemonOnline={daemonOnline} live={visible} />
            <SystemPanel daemonOnline={daemonOnline} live={visible} />
          </div>
          <div className="col">
            <AppsPanel apps={apps} daemonOnline={daemonOnline} exprBusy={expr.busy !== null} />
            <ExpressionsPanel expr={expr} daemonOnline={daemonOnline} controlBusy={controlBusy} appActive={appActive} />
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
        </div>
        <p className="footnote">
          live controller · Reachy daemon via loopback · expose safely with <b>tailscale serve 7861</b>
        </p>
      </div>
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
