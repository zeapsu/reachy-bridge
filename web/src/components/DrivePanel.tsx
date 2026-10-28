import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from "react";
import type { DriveState } from "../hooks/useDrive.ts";
import "./DrivePanel.css";

interface Props {
  drive: DriveState;
  /** Why driving is off right now, or null. */
  blocked: string | null;
}

const KEYS: Record<string, [number, number]> = {
  ArrowLeft: [-1, 0],
  ArrowRight: [1, 0],
  ArrowUp: [0, -1],
  ArrowDown: [0, 1],
};

interface PadProps {
  label: string;
  hint: string;
  disabled: boolean;
  onHold: () => void;
  onRelease: () => void;
  onMove: (x: number, y: number) => void;
}

/** A thumb pad: drag the knob (or hold arrow keys); it springs back on release. */
function Pad({ label, hint, disabled, onHold, onRelease, onMove }: PadProps) {
  const [knob, setKnob] = useState({ x: 0, y: 0 });
  const [held, setHeld] = useState(false);
  const pad = useRef<HTMLDivElement>(null);
  const keys = useRef(new Set<string>());

  const place = (x: number, y: number) => {
    const length = Math.hypot(x, y);
    const [cx, cy] = length > 1 ? [x / length, y / length] : [x, y];
    setKnob({ x: cx, y: cy });
    onMove(cx, cy);
  };
  const fromPointer = (event: PointerEvent) => {
    const rect = pad.current!.getBoundingClientRect();
    const r = rect.width / 2;
    place((event.clientX - rect.left - r) / r, (event.clientY - rect.top - r) / r);
  };
  const letGo = () => {
    setHeld(false);
    keys.current.clear();
    place(0, 0);
    onRelease();
  };
  const fromKeys = () => {
    let x = 0;
    let y = 0;
    for (const key of keys.current) {
      x += KEYS[key][0];
      y += KEYS[key][1];
    }
    place(x, y);
  };

  return (
    <div className="pad-wrap">
      <div
        ref={pad}
        className={`pad${held ? " held" : ""}`}
        role="application"
        aria-label={`${label}: ${hint}`}
        aria-disabled={disabled}
        tabIndex={disabled ? -1 : 0}
        onPointerDown={(event) => {
          if (disabled || event.button !== 0) return;
          event.currentTarget.setPointerCapture(event.pointerId);
          setHeld(true);
          onHold();
          fromPointer(event);
        }}
        onPointerMove={(event) => {
          if (held) fromPointer(event);
        }}
        onPointerUp={letGo}
        onPointerCancel={letGo}
        onLostPointerCapture={() => held && letGo()}
        onKeyDown={(event: KeyboardEvent) => {
          if (disabled || !(event.key in KEYS)) return;
          event.preventDefault();
          if (!keys.current.size) onHold();
          keys.current.add(event.key);
          fromKeys();
        }}
        onKeyUp={(event: KeyboardEvent) => {
          if (!keys.current.delete(event.key)) return;
          if (keys.current.size) fromKeys();
          else letGo();
        }}
        onBlur={() => keys.current.size && letGo()}
      >
        <span className="pad-cross" aria-hidden="true"></span>
        <span
          className="pad-knob"
          aria-hidden="true"
          style={{ transform: `translate(calc(-50% + ${knob.x * 34}cqw), calc(-50% + ${knob.y * 34}cqw))` }}
        ></span>
      </div>
      <span className="pad-label">{label}</span>
    </div>
  );
}

const SLIDER_KEYS = new Set(["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End"]);

interface AntennaProps {
  side: "Left" | "Right";
  disabled: boolean;
  onHold: () => void;
  onRelease: () => void;
  onMove: (value: number) => void;
}

/** An antenna slider: -100..100 around park, back to park on release. */
function Antenna({ side, disabled, onHold, onRelease, onMove }: AntennaProps) {
  const [value, setValue] = useState(0);
  const held = useRef(false);
  const id = `drive-antenna-${side.toLowerCase()}`;
  const set = (next: number) => {
    setValue(next);
    onMove(next / 100);
  };
  const grab = () => {
    held.current = true;
    onHold();
  };
  const letGo = () => {
    if (!held.current) return;
    held.current = false;
    set(0);
    onRelease();
  };
  // A drag that ends outside the slider (or turns into a pan) still lets go.
  const release = useRef(letGo);
  release.current = letGo;
  useEffect(() => {
    const up = () => release.current();
    addEventListener("pointerup", up);
    addEventListener("pointercancel", up);
    return () => {
      removeEventListener("pointerup", up);
      removeEventListener("pointercancel", up);
    };
  }, []);
  return (
    <div className="slider">
      <label htmlFor={id}>
        {side} antenna <output htmlFor={id}>{value > 0 ? `+${value}` : value}</output>
      </label>
      <input
        type="range"
        id={id}
        min="-100"
        max="100"
        value={value}
        disabled={disabled}
        style={{ "--fill": `${(value + 100) / 2}%` } as CSSProperties}
        onPointerDown={grab}
        onKeyDown={(event) => {
          if (SLIDER_KEYS.has(event.key)) grab();
        }}
        // Only a held slider moves its antenna, so a stray change can't leave it off park.
        onChange={(event) => held.current && set(Number(event.target.value))}
        onKeyUp={(event) => SLIDER_KEYS.has(event.key) && letGo()}
        onBlur={letGo}
      />
    </div>
  );
}

/**
 * Manual drive (#152): the head pad aims the head, the body pad turns the
 * body and raises or lowers the head, and the sliders swing the antennas.
 * Everything springs back on release, and the robot goes home once it has.
 */
export function DrivePanel({ drive, blocked }: Props) {
  const disabled = blocked !== null;
  const antennas = useRef<[number, number]>([0, 0]);
  const moveAntenna = (index: 0 | 1) => (value: number) => {
    antennas.current = index === 0 ? [value, antennas.current[1]] : [antennas.current[0], value];
    drive.move({ antennas: antennas.current });
  };

  const status = disabled ? "off" : drive.phase === "live" ? "driving" : drive.phase === "connecting" ? "connecting" : "ready";
  return (
    <div className="pane drive">
      <span className="chip">
        <span className={`dot ${status === "driving" ? "live" : status === "connecting" ? "loading" : status === "ready" ? "ok" : ""}`}></span>
        {status}
      </span>
      <p className={`note${drive.error && !disabled ? " error" : ""}`} id="drive-hint">
        {blocked ?? drive.error ?? "Hold a pad and drag. Let go and the robot goes home."}
      </p>
      <div className="pads">
        <Pad
          label="Head"
          hint="drag to look around"
          disabled={disabled}
          onHold={() => drive.hold("head")}
          onRelease={() => drive.release("head")}
          onMove={(x, y) => drive.move({ head: { x, y } })}
        />
        <Pad
          label="Body"
          hint="drag sideways to turn, up or down to raise or lower the head"
          disabled={disabled}
          onHold={() => drive.hold("body")}
          onRelease={() => drive.release("body")}
          onMove={(x, y) => drive.move({ body: { x, y } })}
        />
      </div>
      <div className="group">
        {(["Left", "Right"] as const).map((side, i) => (
          <Antenna
            key={side}
            side={side}
            disabled={disabled}
            onHold={() => drive.hold(side)}
            onRelease={() => drive.release(side)}
            onMove={moveAntenna(i as 0 | 1)}
          />
        ))}
      </div>
      <button className="btn" type="button" disabled={drive.phase === "idle"} onClick={drive.home}>
        Home
      </button>
    </div>
  );
}
