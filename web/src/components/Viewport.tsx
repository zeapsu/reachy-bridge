import { useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import type { RobotState } from "../api.ts";
import { MODE_BADGE, MODE_META, modeName, type ViewMode } from "../logic/camera.ts";
import { poseFrame, type PoseFrame } from "../logic/pose.ts";
import type { PowerView } from "../logic/power.ts";
import { CameraController } from "./cameraController.ts";
import type { RobotView } from "./robotView.ts";

export type PoseSink = (state: RobotState) => void;

interface Props {
  daemonOnline: boolean;
  visible: boolean;
  power: PowerView;
  onPower: () => void;
  /** The viewport installs its 10 Hz pose writer here. */
  poseSink: RefObject<PoseSink | null>;
}

const MODES: { mode: ViewMode; title: string; label: string; icon: ReactNode }[] = [
  {
    mode: "camera",
    title: "Live camera",
    label: "Camera",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <path d="M3 7h3l2-2h6l2 2h3a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2Z"></path>
        <circle cx="12" cy="13" r="3.5"></circle>
      </svg>
    ),
  },
  {
    mode: "3d",
    title: "3D view",
    label: "3D",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <path d="M12 2 21 7v10l-9 5-9-5V7l9-5Z"></path>
        <path d="M12 12 21 7M12 12v10M12 12 3 7"></path>
      </svg>
    ),
  },
];

type Cache = Partial<Record<keyof PoseFrame, unknown>>;

export function Viewport({ daemonOnline, visible, power, onPower, poseSink }: Props) {
  const [mode, setMode] = useState<ViewMode>("3d");
  const [meta, setMeta] = useState(MODE_META["3d"]);

  const canvas = useRef<HTMLCanvasElement>(null);
  const daemonOnlineRef = useRef(daemonOnline);
  daemonOnlineRef.current = daemonOnline;
  const robot = useRef<RobotView | null>(null);
  const hud = useRef<HTMLDivElement>(null);
  const rDoa = useRef<HTMLSpanElement>(null);
  const rRpy = useRef<HTMLSpanElement>(null);
  const rYaw = useRef<HTMLSpanElement>(null);
  const rAnt = useRef<HTMLSpanElement>(null);
  const cache = useRef<Cache>({});

  const img = useRef<HTMLImageElement>(null);
  const placeholder = useRef<HTMLParagraphElement>(null);
  const camera = useRef<CameraController | null>(null);

  const running = mode === "3d" && visible;
  const runningRef = useRef(running);
  runningRef.current = running;

  // three.js is most of the bundle, so the 3D view loads after first paint.
  useEffect(() => {
    let view: RobotView | null = null;
    let cancelled = false;
    void import("./robotView.ts").then(({ RobotView }) => {
      if (cancelled || !canvas.current) return;
      view = new RobotView(canvas.current);
      if (!daemonOnlineRef.current) view.reset();
      view.setRunning(runningRef.current);
      robot.current = view;
    });
    return () => {
      cancelled = true;
      view?.dispose();
      robot.current = null;
    };
  }, []);

  useEffect(() => {
    robot.current?.setRunning(running);
  }, [running]);

  // Pose writer: the 3D view takes every sample; the HUD touches the DOM only
  // for values that changed since last frame.
  useEffect(() => {
    const guard = <K extends keyof PoseFrame>(key: K, value: PoseFrame[K], write: (v: PoseFrame[K]) => void) => {
      if (value !== cache.current[key]) {
        cache.current[key] = value;
        write(value);
      }
    };
    poseSink.current = (payload) => {
      if (!payload || typeof payload !== "object") return;
      robot.current?.setState(payload);
      const f = poseFrame(payload);
      guard("rpy", f.rpy, (v) => rRpy.current && (rRpy.current.textContent = v));
      guard("yaw", f.yaw, (v) => rYaw.current && (rYaw.current.textContent = v));
      guard("ant", f.ant, (v) => rAnt.current && (rAnt.current.textContent = v));
      guard("doa", f.doa, (v) => rDoa.current && (rDoa.current.textContent = v));
      guard("speaking", f.speaking, (v) => hud.current?.classList.toggle("speaking", v));
    };
    return () => {
      poseSink.current = null;
    };
  }, [poseSink]);

  // Offline: blank the readouts, park the robot, and invalidate the change
  // cache so a reconnect with an unchanged pose still repaints.
  useEffect(() => {
    canvas.current?.classList.toggle("offline", !daemonOnline);
    if (daemonOnline) return;
    robot.current?.reset();
    if (rRpy.current) rRpy.current.textContent = "—/—/—";
    if (rYaw.current) rYaw.current.textContent = "—";
    if (rAnt.current) rAnt.current.textContent = "—/—";
    if (rDoa.current) rDoa.current.textContent = "—°";
    hud.current?.classList.remove("speaking");
    cache.current = {};
  }, [daemonOnline]);

  useEffect(() => {
    if (!img.current || !placeholder.current) return;
    const controller = new CameraController(img.current, placeholder.current, setMeta);
    camera.current = controller;
    const nudge = setInterval(() => controller.nudge(), 1000);
    return () => {
      clearInterval(nudge);
      controller.dispose();
      camera.current = null;
    };
  }, []);

  useEffect(() => {
    camera.current?.update({ selected: mode === "camera", daemonOnline, visible });
  }, [mode, daemonOnline, visible]);

  const selectMode = (next: ViewMode) => {
    if (next === mode) return;
    setMode(next);
    setMeta(MODE_META[next]);
  };

  return (
    <section className="glass viewport">
      <div className="vp-stage" id="vp">
        <div className={`vp-layer${mode === "camera" ? " active" : ""}`} data-mode="camera">
          <img id="camera-stream" ref={img} alt="" hidden aria-hidden="true" />
          <p className="empty-state" id="camera-placeholder" ref={placeholder}>
            Select live camera to connect
          </p>
        </div>
        <div className={`vp-layer${mode === "3d" ? " active" : ""}`} data-mode="3d">
          <canvas id="robot-3d" ref={canvas} aria-label="Live 3D view of the robot's pose"></canvas>
          <p className="rig-note" id="rig-note" hidden={daemonOnline}>
            Daemon offline
          </p>
        </div>

        <div className={`vp-power${power.powerClass ? ` ${power.powerClass}` : ""}`} id="vp-power">
          <button
            id="daemon-power"
            type="button"
            title={power.title}
            aria-label={power.title}
            disabled={power.disabled}
            onClick={onPower}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden="true">
              <path d="M12 3.2v8" />
              <path d="M6.5 6.6a8 8 0 1 0 11 0" />
            </svg>
          </button>
          <span className="vp-power-label" id="vp-power-label" aria-live="polite" hidden={!power.label}>
            {power.label}
          </span>
        </div>

        <div className="vp-badge" id="vp-badge">
          <span className={`rec${daemonOnline ? " ok" : ""}`} id="vp-rec"></span>
          <span id="vp-badge-t">{MODE_BADGE[mode]}</span>
        </div>
        <div className="vp-switch" id="vp-switch" aria-label="Viewport mode">
          {MODES.map((m) => (
            <button
              key={m.mode}
              type="button"
              className={mode === m.mode ? "active" : undefined}
              data-mode={m.mode}
              title={m.title}
              aria-label={m.label}
              onClick={() => selectMode(m.mode)}
            >
              {m.icon}
            </button>
          ))}
        </div>
        <div className="hud" ref={hud}>
          <div className="hud-readout">
            <span className="k">doa</span>
            <span className="v amber" id="r-doa" ref={rDoa}>—°</span>
            <span className="k">head rpy°</span>
            <span className="v" id="r-rpy" ref={rRpy}>—/—/—</span>
            <span className="k">body yaw</span>
            <span className="v" id="r-yaw" ref={rYaw}>—</span>
            <span className="k">ant L/R</span>
            <span className="v" id="r-ant" ref={rAnt}>—/—</span>
          </div>
        </div>
      </div>
      <div className="vp-foot">
        <span>
          viewport · <span className="v" id="vp-name">{modeName(mode)}</span>
        </span>
        <span id="vp-meta">{meta}</span>
      </div>
    </section>
  );
}
