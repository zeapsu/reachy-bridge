import { useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import type { RobotState } from "../api.ts";
import { MODE_BADGE, MODE_META, modeName, type ViewMode } from "../logic/camera.ts";
import { doaArc, poseFrame, type PoseFrame } from "../logic/pose.ts";
import type { PowerView } from "../logic/power.ts";
import { CameraController } from "./cameraController.ts";
import { SimCanvas } from "./SimCanvas.tsx";

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
    mode: "sim",
    title: "Simulation",
    label: "Simulation",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <path d="M12 2 21 7v10l-9 5-9-5V7l9-5Z"></path>
        <path d="M12 12 21 7M12 12v10M12 12 3 7"></path>
      </svg>
    ),
  },
  {
    mode: "schematic",
    title: "Schematic",
    label: "Schematic",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <circle cx="12" cy="13" r="6"></circle>
        <path d="M8 8 6 3M16 8l2-5M12 7V2"></path>
      </svg>
    ),
  },
];

type Cache = Partial<Record<keyof PoseFrame, unknown>>;

export function Viewport({ daemonOnline, visible, power, onPower, poseSink }: Props) {
  const [mode, setMode] = useState<ViewMode>("schematic");
  const [meta, setMeta] = useState(MODE_META.schematic);

  const rig = useRef<SVGSVGElement>(null);
  const head = useRef<SVGGElement>(null);
  const body = useRef<SVGGElement>(null);
  const antL = useRef<SVGGElement>(null);
  const antR = useRef<SVGGElement>(null);
  const arc = useRef<SVGPathElement>(null);
  const tick = useRef<SVGCircleElement>(null);
  const rDoa = useRef<HTMLSpanElement>(null);
  const rRpy = useRef<HTMLSpanElement>(null);
  const rYaw = useRef<HTMLSpanElement>(null);
  const rAnt = useRef<HTMLSpanElement>(null);
  const cache = useRef<Cache>({});

  const img = useRef<HTMLImageElement>(null);
  const placeholder = useRef<HTMLParagraphElement>(null);
  const camera = useRef<CameraController | null>(null);

  // Pose writer: touch the DOM only for values that changed since last frame.
  useEffect(() => {
    const guard = <K extends keyof PoseFrame>(key: K, value: PoseFrame[K], write: (v: PoseFrame[K]) => void) => {
      if (value !== cache.current[key]) {
        cache.current[key] = value;
        write(value);
      }
    };
    poseSink.current = (payload) => {
      if (!payload || typeof payload !== "object") return;
      const f = poseFrame(payload);
      guard("rpy", f.rpy, (v) => rRpy.current && (rRpy.current.textContent = v));
      guard("yaw", f.yaw, (v) => rYaw.current && (rYaw.current.textContent = v));
      guard("ant", f.ant, (v) => rAnt.current && (rAnt.current.textContent = v));
      guard("doa", f.doa, (v) => rDoa.current && (rDoa.current.textContent = v));
      guard("head", f.head, (v) => head.current?.setAttribute("transform", v));
      guard("body", f.body, (v) => body.current?.setAttribute("transform", v));
      guard("antL", f.antL, (v) => antL.current?.setAttribute("transform", v));
      guard("antR", f.antR, (v) => antR.current?.setAttribute("transform", v));
      guard("speaking", f.speaking, (v) => rig.current?.classList.toggle("speaking", v));
      guard("doaDeg", f.doaDeg, (v) => {
        if (v == null) {
          arc.current?.setAttribute("d", "");
          return;
        }
        const a = doaArc(v);
        arc.current?.setAttribute("d", a.d);
        tick.current?.setAttribute("cx", a.tickX);
        tick.current?.setAttribute("cy", a.tickY);
      });
    };
    return () => {
      poseSink.current = null;
    };
  }, [poseSink]);

  // Offline: blank the readouts, park the rig, and invalidate the change
  // cache so a reconnect with an unchanged pose still repaints.
  useEffect(() => {
    rig.current?.classList.toggle("offline", !daemonOnline);
    if (daemonOnline) return;
    if (rRpy.current) rRpy.current.textContent = "—/—/—";
    if (rYaw.current) rYaw.current.textContent = "—";
    if (rAnt.current) rAnt.current.textContent = "—/—";
    if (rDoa.current) rDoa.current.textContent = "—°";
    for (const el of [head.current, body.current, antL.current, antR.current]) el?.removeAttribute("transform");
    arc.current?.setAttribute("d", "");
    tick.current?.setAttribute("cx", "160");
    tick.current?.setAttribute("cy", "16");
    rig.current?.classList.remove("speaking");
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
        <div className={`vp-layer${mode === "sim" ? " active" : ""}`} data-mode="sim">
          <SimCanvas running={mode === "sim"} />
          <p className="empty-state sim-label">Simulation view — coming soon</p>
        </div>
        <div className={`vp-layer${mode === "schematic" ? " active" : ""}`} data-mode="schematic">
          {/* Transform pivots: head 160,104 · body 160,152 · antL 132,70 · antR 188,70 ·
              DOA c160,108 r92. The breath wrapper keeps CSS breathing off the
              transform attributes written per frame. */}
          <svg viewBox="0 0 320 200" id="rig" ref={rig} preserveAspectRatio="xMidYMid meet" aria-label="Live Reachy pose schematic">
            <defs>
              <radialGradient id="halo" cx="50%" cy="46%" r="55%"><stop offset="0%" stopColor="rgba(111,227,255,0.14)"></stop><stop offset="100%" stopColor="rgba(111,227,255,0)"></stop></radialGradient>
              <linearGradient id="hg" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="rgba(255,255,255,0.2)"></stop><stop offset="100%" stopColor="rgba(255,255,255,0.05)"></stop></linearGradient>
              <linearGradient id="tg" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="rgba(255,255,255,0.11)"></stop><stop offset="100%" stopColor="rgba(255,255,255,0.02)"></stop></linearGradient>
              <radialGradient id="floorGlow" cx="50%" cy="50%" r="50%"><stop offset="0%" stopColor="rgba(111,227,255,0.12)"></stop><stop offset="100%" stopColor="rgba(111,227,255,0)"></stop></radialGradient>
            </defs>
            <circle className="halo" cx="160" cy="108" r="92" fill="url(#halo)"></circle>
            <circle cx="160" cy="108" r="92" fill="none" stroke="rgba(255,255,255,0.10)" strokeWidth="1.5"></circle>
            <ellipse className="floor" cx="160" cy="183" rx="62" ry="8.5" fill="url(#floorGlow)"></ellipse>
            <path id="doaArc" ref={arc} d="" fill="none" stroke="var(--amber)" strokeWidth="4" strokeLinecap="round"></path>
            <circle id="doaTick" ref={tick} cx="160" cy="16" r="4" fill="var(--amber)"></circle>
            <g className="breath">
              <g id="body" ref={body}>
                <path className="torso" d="M131 138 C130 154 124 166 120 175 Q118 181 127 181 L193 181 Q202 181 200 175 C196 166 190 154 189 138 Z" fill="url(#tg)" stroke="rgba(255,255,255,0.15)" strokeWidth="1.2"></path>
                <g id="antL" ref={antL}>
                  <line className="ant-glow" x1="132" y1="70" x2="118" y2="26" stroke="var(--cyan)" strokeWidth="7" strokeLinecap="round" opacity="0.2"></line>
                  <line x1="132" y1="70" x2="118" y2="26" stroke="var(--cyan)" strokeWidth="3" strokeLinecap="round"></line>
                  <circle cx="118" cy="26" r="5.5" fill="var(--cyan)"></circle>
                </g>
                <g id="antR" ref={antR}>
                  <line className="ant-glow" x1="188" y1="70" x2="202" y2="26" stroke="var(--cyan)" strokeWidth="7" strokeLinecap="round" opacity="0.2"></line>
                  <line x1="188" y1="70" x2="202" y2="26" stroke="var(--cyan)" strokeWidth="3" strokeLinecap="round"></line>
                  <circle cx="202" cy="26" r="5.5" fill="var(--cyan)"></circle>
                </g>
                <g id="head" ref={head}>
                  <rect x="114" y="64" width="92" height="80" rx="26" fill="url(#hg)" stroke="rgba(255,255,255,0.26)" strokeWidth="1.5"></rect>
                  <g className="eye">
                    <circle cx="143" cy="102" r="10" fill="rgba(5,6,12,0.92)" stroke="rgba(255,255,255,0.2)" strokeWidth="1.2"></circle>
                    <circle className="iris" cx="143" cy="102" r="4.6" fill="none" stroke="var(--cyan)" strokeWidth="1.4" opacity="0.9"></circle>
                    <circle className="pupil" cx="143" cy="102" r="2.2" fill="var(--cyan)"></circle>
                    <circle className="glint" cx="146.4" cy="98.8" r="1.5" fill="rgba(255,255,255,0.85)"></circle>
                    <path className="lid" d="M134.5 101.5 Q143 107.5 151.5 101.5" fill="none" stroke="rgba(255,255,255,0.4)" strokeWidth="2" strokeLinecap="round"></path>
                  </g>
                  {/* right eye is the camera eye — a touch larger, on purpose */}
                  <g className="eye">
                    <circle cx="177" cy="102" r="11" fill="rgba(5,6,12,0.92)" stroke="rgba(255,255,255,0.2)" strokeWidth="1.2"></circle>
                    <circle className="iris" cx="177" cy="102" r="5.2" fill="none" stroke="var(--cyan)" strokeWidth="1.4" opacity="0.9"></circle>
                    <circle className="pupil" cx="177" cy="102" r="2.4" fill="var(--cyan)"></circle>
                    <circle className="glint" cx="180.8" cy="98.6" r="1.6" fill="rgba(255,255,255,0.85)"></circle>
                    <path className="lid" d="M167.5 101.5 Q177 108 186.5 101.5" fill="none" stroke="rgba(255,255,255,0.4)" strokeWidth="2" strokeLinecap="round"></path>
                  </g>
                </g>
              </g>
            </g>
          </svg>
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
        <div className="hud">
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
