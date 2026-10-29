import { useEffect, useState, type FormEvent } from "react";
import { apiJson, paths } from "../api.ts";
import { useLens } from "../glass.ts";
import type { ControlApi } from "../hooks/useControl.ts";
import type { PairedDevice } from "../logic/auth.ts";
import { SLOTS, clock, slotText } from "../logic/control.ts";

/**
 * The admin's side of control time (#221): answer requests, grant and
 * revoke time. Renders only for an admin in admin mode.
 */
export function ControlPanel({ control }: { control: ControlApi }) {
  const sheet = useLens<HTMLElement>();
  const [people, setPeople] = useState<string[]>([]);
  const [person, setPerson] = useState("");
  const [slot, setSlot] = useState(1);
  const view = control.view;
  const admin = Boolean(view?.admin_mode && view.you?.admin);

  useEffect(() => {
    if (!admin) return;
    apiJson<{ devices: PairedDevice[] }>(paths.authDevices)
      .then(({ devices }) => setPeople([...new Set(devices.map((d) => d.person || d.name))].sort()))
      .catch(() => setPeople([]));
  }, [admin]);

  if (!admin || !view) return null;
  const { act, busy, error } = control;
  const requests = view.requests ?? [];
  const grants = view.grants ?? [];

  const grant = (event: FormEvent) => {
    event.preventDefault();
    if (person) void act(paths.controlGrants, "POST", { person, minutes: SLOTS[slot].minutes });
  };

  return (
    <section className="glass sheet" data-lens ref={sheet} id="control-panel" aria-labelledby="control-title">
      <header>
        <h2 id="control-title">Control time</h2>
      </header>
      {requests.length > 0 && (
        <ul className="devices-list" aria-label="Requests">
          {requests.map((r) => (
            <li key={r.person}>
              <span className="devices-name">
                {r.person} asks for {slotText(r.minutes)}
              </span>
              <span className="devices-meta">
                at {clock(r.at)}
                {r.note ? ` · ${r.note}` : ""}
              </span>
              <span className="row-actions">
                <button
                  className="btn primary press"
                  type="button"
                  disabled={busy}
                  onClick={() => void act(paths.controlGrants, "POST", { person: r.person, minutes: r.minutes })}
                >
                  Grant
                </button>
                <button className="btn press" type="button" disabled={busy} onClick={() => void act(paths.controlRefuse(r.person))}>
                  Refuse
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}
      <form className="devices-approve" onSubmit={grant}>
        <select className="field" aria-label="Person" value={person} onChange={(e) => setPerson(e.target.value)}>
          <option value="">person…</option>
          {people.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
        <select className="field" aria-label="For how long" value={slot} onChange={(e) => setSlot(Number(e.target.value))}>
          {SLOTS.map((s, i) => (
            <option key={s.label} value={i}>
              {s.label}
            </option>
          ))}
        </select>
        <button className="btn primary press" type="submit" disabled={busy || !person}>
          Grant
        </button>
      </form>
      {error && <p className="note error">{error}</p>}
      {grants.length === 0 ? (
        <p className="note">No control time granted. You can always take control yourself.</p>
      ) : (
        <ul className="devices-list" aria-label="Grants">
          {grants.map((g) => (
            <li key={g.id}>
              <span className="devices-name">{g.person}</span>
              <span className="devices-meta">
                {g.from > (view.now ?? 0) ? `from ${clock(g.from)} ` : ""}
                {g.until === null ? "no end" : `until ${clock(g.until)}`}
              </span>
              <button className="btn danger press" type="button" disabled={busy} onClick={() => void act(paths.controlGrant(g.id), "DELETE")}>
                Revoke
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
