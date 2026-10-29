import { useEffect, useRef, useState, type ReactNode } from "react";
import { paths } from "../api.ts";
import type { ControlApi } from "../hooks/useControl.ts";
import { SLOTS, clock, controlNotice, controlState, showStrip, type Asking, type ControlState } from "../logic/control.ts";
import "./ControlStrip.css";

/**
 * Who drives the robot (#221), one line inside a view that acts on it, and
 * only when this browser can't just act: someone else is in control, or
 * nobody is and this person has to ask. Nothing outside admin mode, while
 * this browser is in control, or when its first action can take control.
 */
export function ControlStrip({ control }: { control: ControlApi }) {
  const [slot, setSlot] = useState(1);
  const state = controlState(control.view);
  if (!showStrip(state)) return null;
  const { act, busy, error } = control;

  const ask = (asking: Asking): [string, ReactNode] => {
    switch (asking.kind) {
      case "can_ask":
        return [
          "Ask the admin for control time.",
          <span className="control-ask" key="ask">
            <select className="field" aria-label="How long" value={slot} onChange={(e) => setSlot(Number(e.target.value))}>
              {SLOTS.map((s, i) => (
                <option key={s.label} value={i}>
                  {s.label}
                </option>
              ))}
            </select>
            <button
              className="btn primary press"
              type="button"
              disabled={busy}
              onClick={() => void act(paths.controlRequest, "POST", { minutes: SLOTS[slot].minutes })}
            >
              Ask
            </button>
          </span>,
        ];
      case "asked":
        return [
          "Waiting for the admin.",
          <button className="btn press" type="button" key="cancel" disabled={busy} onClick={() => void act(paths.controlRequest, "DELETE")}>
            Cancel
          </button>,
        ];
      case "cooldown":
        return [`You can ask again at ${clock(asking.until)}.`, null];
      case "none":
        return ["", null];
    }
  };

  let text = "";
  let action: ReactNode = null;
  if (state.kind === "other") {
    text = `${state.person} is in control${state.until === null ? "" : ` until ${clock(state.until)}`}.`;
    if (state.canTakeOver) {
      action = (
        <button className="btn press" type="button" disabled={busy} onClick={() => void act(paths.controlTake)}>
          Take over
        </button>
      );
    } else {
      const [more, button] = ask(state.asking);
      if (more) text += ` ${more}`;
      action = button;
    }
  } else if (state.kind === "free") {
    [text, action] = ask(state.asking);
  }

  return (
    <div className="control-strip" role="status" aria-live="polite">
      <p className="control-text">{text}</p>
      {action}
      {error && (
        <p className="note error control-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

const TOAST_MS = 4000;

/**
 * A notice for a few seconds when who drives changes (#221): you took
 * control (with Release), someone took over, your time is nearly up.
 * `nudge` changing shows "You're in control" again, from the status pill.
 */
export function ControlToast({ control, nudge }: { control: ControlApi; nudge: number }) {
  const state = controlState(control.view);
  const key = JSON.stringify(state);
  const prev = useRef<ControlState>({ kind: "off" });
  const [toast, setToast] = useState<{ text: string; release: boolean } | null>(null);

  useEffect(() => {
    const next: ControlState = JSON.parse(key);
    const text = controlNotice(prev.current, next);
    prev.current = next;
    if (text) setToast({ text, release: next.kind === "mine" });
  }, [key]);

  useEffect(() => {
    if (nudge === 0 || prev.current.kind !== "mine") return;
    const mine = prev.current;
    setToast({ text: mine.until === null ? "You're in control" : `You're in control until ${clock(mine.until)}`, release: true });
  }, [nudge]);

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), TOAST_MS);
    return () => clearTimeout(timer);
  }, [toast]);

  return (
    <div aria-live="polite">
      {toast && (
        <div className="glass control-toast" role="status">
          <span>{toast.text}</span>
          {toast.release && (
            <button
              className="btn press"
              type="button"
              disabled={control.busy}
              onClick={() => {
                setToast(null);
                void control.act(paths.controlRelease);
              }}
            >
              Release
            </button>
          )}
        </div>
      )}
    </div>
  );
}
