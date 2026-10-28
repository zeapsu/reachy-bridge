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

/**
 * The Apps tab in sim mode. The sim daemon has no apps, and the robot's
 * installed and pinned apps aren't the sim's, so neither list is shown.
 */
export function SimAppsNote() {
  const sheet = useLens<HTMLElement>();
  return (
    <section className="glass sheet" data-lens ref={sheet} aria-labelledby="sim-apps-title">
      <header>
        <h2 id="sim-apps-title">Apps</h2>
      </header>
      <p className="note">Apps are off in sim mode. Go back to the robot from System → Simulation to install and run them.</p>
    </section>
  );
}
