import type { ControlApi } from "../hooks/useControl.ts";
import type { SimState } from "../hooks/useSim.ts";
import type { SimSoundsState } from "../hooks/useSimSounds.ts";
import { simView } from "../logic/sim.ts";
import { ControlStrip } from "./ControlStrip.tsx";
import { ROW_ICONS, SettingsRow } from "./SettingsRow.tsx";

const OFF = ["stopped", "not_initialized", "error", "offline"];

/** Sim mode (#149): drive a simulated Reachy instead of the real one. */
export function SimRows({
  sim,
  sounds,
  powerStatus,
  control,
}: {
  sim: SimState;
  sounds: SimSoundsState;
  powerStatus: string | null;
  control: ControlApi;
}) {
  const view = simView(sim.target, sim.switching, powerStatus !== null && OFF.includes(powerStatus));
  const on = sim.target === "sim";
  const soundsOff = !on || sim.switching;
  const sub = sim.switching ? view.action : view.hint;
  return (
    <>
      {/* Switching acts on the robot: take control here, not on the Robot tab. */}
      <ControlStrip control={control} />
      <ul className="set-list">
        <SettingsRow
          icon={ROW_ICONS.sim}
          label="Simulation"
          labelId="sim-label"
          sub={sub}
          subId="sim-hint"
          off={view.disabled && !sim.switching}
        >
          {sim.switching ? <span className="spin" aria-hidden="true"></span> : null}
          <button
            type="button"
            className="switch"
            role="switch"
            id="sim-toggle"
            aria-labelledby="sim-label"
            aria-describedby={sub ? "sim-hint" : undefined}
            aria-checked={on}
            disabled={view.disabled}
            onClick={sim.toggle}
          ></button>
        </SettingsRow>
        <SettingsRow icon={ROW_ICONS.sound} label="Play its sounds on this device" labelId="sim-sound-label" off={soundsOff}>
          <button
            className="switch"
            type="button"
            role="switch"
            id="sim-sound"
            aria-labelledby="sim-sound-label"
            aria-checked={on && sounds.on}
            disabled={soundsOff}
            onClick={() => sounds.setOn(!sounds.on)}
          ></button>
        </SettingsRow>
      </ul>
      <p className="note error" role="alert">
        {sim.error}
      </p>
    </>
  );
}
