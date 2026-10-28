import { useLens } from "../glass.ts";
import type { SimState } from "../hooks/useSim.ts";
import { simView } from "../logic/sim.ts";

const OFF = ["stopped", "not_initialized", "error", "offline"];

/** Sim mode (#149): drive a simulated Reachy instead of the real one. */
export function SimPanel({ sim, powerStatus }: { sim: SimState; powerStatus: string | null }) {
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
      <p className="note error" role="alert">
        {sim.error}
      </p>
    </section>
  );
}
