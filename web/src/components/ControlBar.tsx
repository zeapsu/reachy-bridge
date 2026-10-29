import { useState, type ReactNode } from "react";
import { paths } from "../api.ts";
import type { ControlApi } from "../hooks/useControl.ts";
import { SLOTS, clock, controlState } from "../logic/control.ts";
import "./ControlBar.css";

/**
 * Who drives the robot (#221), above the robot controls. Shows nothing
 * outside admin mode.
 */
export function ControlBar({ control }: { control: ControlApi }) {
  const [slot, setSlot] = useState(1);
  const state = controlState(control.view);
  if (state.kind === "off") return null;
  const { act, busy, error } = control;
  const take = () => void act(paths.controlTake);

  let text: string;
  let action: ReactNode = null;
  switch (state.kind) {
    case "mine":
      text = state.until === null ? "You're in control." : `You're in control until ${clock(state.until)}.`;
      if (state.endingSoon) text += " Your time ends in under a minute.";
      action = (
        <button className="btn press" type="button" disabled={busy} onClick={() => void act(paths.controlRelease)}>
          Release
        </button>
      );
      break;
    case "other":
      text = `${state.person} is in control${state.until === null ? "" : ` until ${clock(state.until)}`}. You can watch.`;
      if (state.canTakeOver) {
        action = (
          <button className="btn press" type="button" disabled={busy} onClick={take}>
            Take over
          </button>
        );
      }
      break;
    case "free":
      if (state.canTake) {
        text = "Nobody is in control.";
        action = (
          <button className="btn primary press" type="button" disabled={busy} onClick={take}>
            Take control
          </button>
        );
      } else {
        text = "Ask the admin for control time to drive the robot.";
        action = (
          <span className="control-ask">
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
              Ask for control
            </button>
          </span>
        );
      }
      break;
    case "asked":
      text = "You asked for control time. Waiting for the admin.";
      action = (
        <button className="btn press" type="button" disabled={busy} onClick={() => void act(paths.controlRequest, "DELETE")}>
          Cancel
        </button>
      );
      break;
    case "cooldown":
      text = `You can ask for control again at ${clock(state.until)}.`;
      break;
  }

  return (
    <div className={`control-bar${state.kind === "mine" ? " mine" : ""}`} id="control-bar" role="status" aria-live="polite">
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
