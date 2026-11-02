import { useEffect, useRef, useState, type ReactNode } from "react";
import { CONTROL_NEEDED_EVENT, NEEDS_CONTROL, paths } from "../api.ts";
import type { ControlApi } from "../hooks/useControl.ts";
import { controlNotice, controlState, showStrip, type ControlState } from "../logic/control.ts";
import "./ControlStrip.css";

const sentence = (text: string) => (text ? `${text[0].toUpperCase()}${text.slice(1)}.` : text);

/**
 * Who drives the robot (#221), one line inside a view that acts on it, and
 * only when this browser can't just act: someone else is in control.
 * Nothing outside admin mode, while this browser is in control, or when its
 * first action can take control, unless an action was refused without
 * taking it (no tap behind it): then it offers Take control.
 */
export function ControlStrip({ control }: { control: ControlApi }) {
  const [refused, setRefused] = useState(false);
  const state = controlState(control.view);
  const free = state.kind === "free";

  useEffect(() => {
    // Only "nobody is in control": a refusal while someone else held it says nothing about later.
    const onRefused = (event: Event) => setRefused((event as CustomEvent<unknown>).detail === NEEDS_CONTROL);
    window.addEventListener(CONTROL_NEEDED_EVENT, onRefused);
    return () => window.removeEventListener(CONTROL_NEEDED_EVENT, onRefused);
  }, []);
  useEffect(() => {
    if (!free) setRefused(false);
  }, [free]);

  const { act, busy } = control;
  // Take over means it; Take control (nobody has it, as far as this view knows) must not, if someone just did.
  const takeButton = (label: string, primary = false, path: string = paths.controlTake) => (
    <button className={`btn press${primary ? " primary" : ""}`} type="button" disabled={busy} onClick={() => void act(path)}>
      {label}
    </button>
  );

  let text = "";
  let action: ReactNode = null;
  if (state.kind === "other" && showStrip(state)) {
    const who = state.elsewhere !== undefined ? `You're in control on ${state.elsewhere || "another device"}` : `${state.person} is in control`;
    text = `${who}.`;
    if (state.canTakeOver) action = takeButton("Take over");
  } else if (state.kind === "free" && showStrip(state)) {
    text = "Log in to drive.";
  } else if (state.kind === "free" && refused) {
    text = "Nobody is in control.";
    action = takeButton("Take control", true, paths.controlTakeIfFree);
  }
  if (!text && !action) return null;

  return (
    <div className="control-strip">
      <p className="control-text">{text}</p>
      {action}
    </div>
  );
}

const TOAST_MS = 4000;
// A tap fires mouseenter on touch screens and no mouseleave until the next tap elsewhere.
const hover = typeof window !== "undefined" && window.matchMedia?.("(hover: hover)").matches;

/**
 * A notice for a few seconds when who drives changes (#221): you took
 * control (with Release), someone took over; and control actions or robot
 * actions refused for control. It stays while hovered or focused.
 */
export function ControlToast({ control }: { control: ControlApi }) {
  const state = controlState(control.view);
  const key = JSON.stringify(state);
  const prev = useRef<ControlState>({ kind: "off" });
  const [toast, setToast] = useState<{ text: string; release: boolean; error?: boolean } | null>(null);
  const [held, setHeld] = useState(false);

  useEffect(() => {
    const next: ControlState = JSON.parse(key);
    const text = controlNotice(prev.current, next);
    prev.current = next;
    if (text) setToast({ text, release: next.kind === "mine" });
  }, [key]);

  // A tap refused because someone else has control: say so wherever the tap was.
  useEffect(() => {
    const onRefused = (event: Event) => {
      const detail = (event as CustomEvent<unknown>).detail;
      if (typeof detail === "string") setToast({ text: sentence(detail), release: false, error: true });
    };
    window.addEventListener(CONTROL_NEEDED_EVENT, onRefused);
    return () => window.removeEventListener(CONTROL_NEEDED_EVENT, onRefused);
  }, []);

  useEffect(() => {
    if (control.error) setToast({ text: control.error, release: false, error: true });
  }, [control.error]);

  useEffect(() => {
    if (!toast || held) return;
    const timer = setTimeout(() => setToast(null), TOAST_MS);
    return () => clearTimeout(timer);
  }, [toast, held]);

  return (
    <>
      <p className="sr-only" role="status">
        {toast?.text ?? ""}
      </p>
      {toast && (
        <div
          className={`glass control-toast${toast.error ? " error" : ""}`}
          onMouseEnter={() => hover && setHeld(true)}
          onMouseLeave={() => setHeld(false)}
          onFocus={() => setHeld(true)}
          onBlur={() => setHeld(false)}
        >
          <span>{toast.text}</span>
          {toast.release && (
            <button
              className="btn press"
              type="button"
              disabled={control.busy}
              onClick={() => {
                setToast(null);
                setHeld(false);
                void control.act(paths.controlRelease);
              }}
            >
              Release
            </button>
          )}
        </div>
      )}
    </>
  );
}
