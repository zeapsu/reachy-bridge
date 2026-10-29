import { useLens } from "../glass.ts";
import type { SimState } from "../hooks/useSim.ts";
import type { SimSoundsState } from "../hooks/useSimSounds.ts";
import { simView } from "../logic/sim.ts";

const OFF = ["stopped", "not_initialized", "error", "offline"];

/** Sim mode (#149): drive a simulated Reachy instead of the real one. */
export function SimPanel({
  sim,
  sounds,
  powerStatus,
}: {
  sim: SimState;
  sounds: SimSoundsState;
  powerStatus: string | null;
}) {
  const sheet = useLens<HTMLElement>();
  const view = simView(sim.target, sim.switching, powerStatus !== null && OFF.includes(powerStatus));
  return (
    <section className="glass sheet" data-lens ref={sheet} aria-labelledby="sim-title">
      <header>
        <h2 id="sim-title">Simulation</h2>
      </header>
      <div className="row">
        <p className="note" id="sim-hint">{view.hint}</p>
        <button
          type="button"
          className={`btn press${sim.target === "sim" ? "" : " primary"}`}
          id="sim-toggle"
          disabled={view.disabled}
          onClick={sim.toggle}
          aria-describedby="sim-hint"
        >
          {view.action}
        </button>
      </div>
      {sim.target === "sim" && !sim.switching ? (
        <div className="row">
          <label htmlFor="sim-sound">Play its sounds on this device</label>
          <button
            className="switch"
            type="button"
            role="switch"
            id="sim-sound"
            aria-checked={sounds.on}
            onClick={() => sounds.setOn(!sounds.on)}
          ></button>
        </div>
      ) : null}
      <p className="note error" role="alert">
        {sim.error}
      </p>
    </section>
  );
}

