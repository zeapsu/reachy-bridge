import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from "react";
import type { DriveState } from "../hooks/useDrive.ts";
import { read, write } from "../logic/appearance.ts";
import { HOLD_KEY, inPad } from "../logic/drive.ts";
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

/** A held arrow key moves a hold-mode knob this far per key event (repeats included). */
const NUDGE = 0.08;

interface PadProps {
  label: string;
  hint: string;
  disabled: boolean;
  /** Hold mode: the knob stays where it is let go, and a drag moves it from there. */
  sticky: boolean;
  /** Each change puts the knob back in the centre. */
  reset: number;
  onHold: () => void;
  onRelease: () => void;
  onMove: (x: number, y: number) => void;
}

/**
 * A thumb pad: drag the knob (or hold arrow keys); it springs back on release.
 * A sticky pad keeps the knob where it was let go instead.
 */
function Pad({ label, hint, disabled, sticky, reset, onHold, onRelease, onMove }: PadProps) {
  const [knob, setKnob] = useState({ x: 0, y: 0 });
  const [held, setHeld] = useState(false);
  const pad = useRef<HTMLDivElement>(null);
  const keys = useRef(new Set<string>());
  const at = useRef({ x: 0, y: 0 });
  /** Knob minus finger at the grab, so a sticky knob doesn't jump to the finger. */
  const grip = useRef({ x: 0, y: 0 });

  const place = (x: number, y: number) => {
    const next = inPad(x, y);
    at.current = next;
    setKnob(next);
    onMove(next.x, next.y);
  };
  const finger = (event: PointerEvent) => {
    const rect = pad.current!.getBoundingClientRect();
    const r = rect.width / 2;
    return { x: (event.clientX - rect.left - r) / r, y: (event.clientY - rect.top - r) / r };
  };
  const fromPointer = (event: PointerEvent) => {
    const f = finger(event);
    place(f.x + grip.current.x, f.y + grip.current.y);
  };
  const letGo = () => {
    setHeld(false);
    keys.current.clear();
    if (!sticky) place(0, 0);
    onRelease();
  };
  useEffect(() => {
    if (!reset) return;
    // Recentred under a finger (the session ended, Home, hold off): the
    // rest of that drag and its keys are ignored, so the knob can't come
    // back off centre with no session behind it.
    setHeld(false);
    keys.current.clear();
    place(0, 0);
    // Only a new reset recentres.
  }, [reset]);
  const fromKeys = (key: string) => {
    if (sticky) {
      place(at.current.x + KEYS[key][0] * NUDGE, at.current.y + KEYS[key][1] * NUDGE);
      return;
    }
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
          const f = finger(event);
          grip.current = sticky ? { x: at.current.x - f.x, y: at.current.y - f.y } : { x: 0, y: 0 };
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
          fromKeys(event.key);
        }}
        onKeyUp={(event: KeyboardEvent) => {
          if (!keys.current.delete(event.key)) return;
          if (!keys.current.size) letGo();
          else if (!sticky) fromKeys(event.key);
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
  /** Hold mode: the slider stays where it is let go. */
  sticky: boolean;
  /** Each change puts the slider back on park. */
  reset: number;
  onHold: () => void;
  onRelease: () => void;
  onMove: (value: number) => void;
}

/** An antenna slider: -100..100 around park, back to park on release unless sticky. */
function Antenna({ side, disabled, sticky, reset, onHold, onRelease, onMove }: AntennaProps) {
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
    if (!sticky) set(0);
    onRelease();
  };
  useEffect(() => {
    if (!reset) return;
    // As for a pad: the rest of a drag under way is ignored.
    held.current = false;
    set(0);
  }, [reset]);
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
 * With hold on, everything stays where it was let go, and so does the robot,
 * until Home (or the session ends, when everything recentres).
 */
export function DrivePanel({ drive, blocked }: Props) {
  const disabled = blocked !== null;
  const [hold, setHold] = useState(() => read(HOLD_KEY) === "1");
  const [reset, setReset] = useState(0);
  const recentre = () => setReset((n) => n + 1);
  // A session that ended sent the robot home: the controls follow.
  const wasLive = useRef(false);
  useEffect(() => {
    if (drive.phase === "idle" && wasLive.current) recentre();
    wasLive.current = drive.phase !== "idle";
  }, [drive.phase]);
  const toggleHold = () => {
    const next = !hold;
    setHold(next);
    write(HOLD_KEY, next ? "1" : null);
    // Hold off: spring back now, and the robot eases home.
    if (!next) recentre();
  };
  const antennas = useRef<[number, number]>([0, 0]);
  const moveAntenna = (index: 0 | 1) => (value: number) => {
    antennas.current = index === 0 ? [value, antennas.current[1]] : [antennas.current[0], value];
    drive.move({ antennas: antennas.current });
  };

  const status = disabled ? "off" : drive.phase === "live" ? "driving" : drive.phase === "connecting" ? "connecting" : "ready";
  return (
    <div className="pane drive">
      <div className="drive-head">
        <span className="chip">
          <span className={`dot ${status === "driving" ? "live" : status === "connecting" ? "loading" : status === "ready" ? "ok" : ""}`}></span>
          {status}
        </span>
        <div className="drive-actions">
          <button
            className={`btn icon hold${hold ? " on" : ""}`}
            type="button"
            aria-pressed={hold}
            aria-label="Hold pose"
            title="Hold pose"
            onClick={toggleHold}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M9 3.5h6l-1 5.5 3 3v1.5H7V12l3-3-1-5.5ZM12 13.5V21"></path>
            </svg>
          </button>
          <button className="btn" type="button" disabled={drive.phase === "idle"} onClick={drive.home}>
            Home
          </button>
        </div>
      </div>
      {(blocked ?? drive.error) && (
        <p className={`note${drive.error && !disabled ? " error" : ""}`}>{blocked ?? drive.error}</p>
      )}
      <div className="pads">
        <Pad
          label="Head"
          hint="drag to look around"
          disabled={disabled}
          sticky={hold}
          reset={reset}
          onHold={() => drive.hold("head")}
          onRelease={() => drive.release("head")}
          onMove={(x, y) => drive.move({ head: { x, y } })}
        />
        <Pad
          label="Body"
          hint="drag sideways to turn, up or down to raise or lower the head"
          disabled={disabled}
          sticky={hold}
          reset={reset}
          onHold={() => drive.hold("body")}
          onRelease={() => drive.release("body")}
          onMove={(x, y) => drive.move({ body: { x, y } })}
        />
      </div>
      <div className="group antennas">
        {(["Left", "Right"] as const).map((side, i) => (
          <Antenna
            key={side}
            side={side}
            disabled={disabled}
            sticky={hold}
            reset={reset}
            onHold={() => drive.hold(side)}
            onRelease={() => drive.release(side)}
            onMove={moveAntenna(i as 0 | 1)}
          />
        ))}
      </div>
    </div>
  );
}
