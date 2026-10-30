import { useCallback, useEffect, useState, type FormEvent } from "react";
import { apiJson, errorText, paths } from "../api.ts";
import { useLens } from "../glass.ts";
import { devicesView, normalizeCode, seenText, type AuthStatus, type DevicesView, type PairedDevice } from "../logic/auth.ts";
import { PairCode } from "./Login.tsx";
import "./Login.css";
import "./DevicesPanel.css";

/**
 * Paired browsers (#141, #221). Before an admin exists it offers to make
 * this browser the admin; an admin approves login codes and revokes
 * devices; a member sees and can log out its own device. Renders nothing
 * when there's nothing to offer (the CLI's key, or an older controller
 * without LAN mode).
 */
export function DevicesPanel() {
  const [view, setView] = useState<DevicesView>("hidden");
  const [devices, setDevices] = useState<PairedDevice[]>([]);
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [person, setPerson] = useState("");
  const [hint, setHint] = useState<{ text: string; error: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  // No admin yet (LAN mode can manage devices before one exists).
  const [noAdmin, setNoAdmin] = useState(false);
  const [settingUp, setSettingUp] = useState(false);
  const sheet = useLens<HTMLElement>();

  const reload = useCallback(async () => {
    try {
      setDevices((await apiJson<{ devices: PairedDevice[] }>(paths.authDevices)).devices);
    } catch (error) {
      setHint({ text: errorText(error, "Couldn't load devices."), error: true });
    }
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        const status = await apiJson<AuthStatus>(paths.authStatus);
        const next = devicesView(status);
        setNoAdmin(status.admin_mode === false);
        setView(next);
        if (next === "manage" || next === "own") await reload();
      } catch {
        setView("hidden");
      }
    })();
  }, [reload]);

  if (view === "hidden") return null;

  const approve = async (event: FormEvent) => {
    event.preventDefault();
    const normalized = normalizeCode(code);
    if (!normalized) {
      setHint({ text: "The code is 6 digits.", error: true });
      return;
    }
    setBusy(true);
    try {
      const body = { code: normalized, name: name.trim() || undefined, person: person.trim() || undefined };
      await apiJson(paths.authApprove, { method: "POST", body: JSON.stringify(body) });
      setCode("");
      setName("");
      setPerson("");
      setHint({ text: "Approved. The browser logs in within a few seconds.", error: false });
      // The device appears once its browser collects the token.
      setTimeout(() => void reload(), 3000);
    } catch (error) {
      setHint({ text: errorText(error, "Approval failed."), error: true });
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (device: PairedDevice) => {
    const question = device.current
      ? `Log this browser (${device.name}) out? It will need a new code to log in.`
      : `Revoke ${device.name}?`;
    if (!window.confirm(question)) return;
    try {
      await apiJson(paths.authDevice(device.id), { method: "DELETE" });
      setHint({ text: `Revoked ${device.name}.`, error: false });
      if (device.current) window.location.reload();
      else await reload();
    } catch (error) {
      setHint({ text: errorText(error, "Revoke failed."), error: true });
    }
  };

  const now = Date.now();
  // The controller won't revoke the last admin from a dashboard (the CLI on the robot can).
  const admins = devices.filter((device) => device.role === "admin").length;
  const lastAdmin = (device: PairedDevice) => device.role === "admin" && admins === 1;
  return (
    <section className="glass sheet devices-panel" data-lens ref={sheet} id="devices-panel" aria-labelledby="devices-title">
      <header>
        <h2 id="devices-title">Devices</h2>
      </header>
      {noAdmin && (
        <div className="devices-setup">
          <p className="note">
            No admin yet: anyone who reaches this dashboard can drive the robot. Make this browser the admin, and
            every other browser, tailnet included, will need a code you approve.
          </p>
          {settingUp ? (
            <PairCode admin />
          ) : (
            <button className="btn primary press" type="button" onClick={() => setSettingUp(true)}>
              Make this browser the admin
            </button>
          )}
        </div>
      )}
      {view === "manage" && (
        <form className="devices-approve" onSubmit={(event) => void approve(event)}>
          <input
            className="field"
            aria-label="Login code"
            inputMode="numeric"
            autoComplete="off"
            placeholder="123 456"
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
          <input className="field" aria-label="Device name (optional)" placeholder="device (optional)" maxLength={64} value={name} onChange={(e) => setName(e.target.value)} />
          <input className="field" aria-label="Person (optional)" placeholder="person (optional)" maxLength={64} list="devices-people" value={person} onChange={(e) => setPerson(e.target.value)} />
          {/* Existing people, so a second device joins its person (control moves between their devices) rather than a near-duplicate name. */}
          <datalist id="devices-people">
            {[...new Set(devices.map((d) => d.person || d.name))].map((p) => (
              <option key={p} value={p} />
            ))}
          </datalist>
          <button className="btn primary press" type="submit" disabled={busy}>
            Approve
          </button>
        </form>
      )}
      {hint && <p className={`note ${hint.error ? "error" : "ok"}`}>{hint.text}</p>}
      {view !== "setup" &&
        (devices.length === 0 ? (
          <p className="note">No paired browsers.</p>
        ) : (
          <ul className="devices-list">
            {devices.map((device) => (
              <li key={device.id}>
                <span className="devices-name">
                  {device.name}
                  {device.person && device.person !== device.name && <span className="devices-person"> · {device.person}</span>}
                  {device.role === "admin" && <span className="devices-role"> · admin</span>}
                  {device.current && <span className="devices-current"> · this browser</span>}
                </span>
                <span className="devices-meta">
                  {seenText(device.last_seen, now)} · {device.peer}
                </span>
                {lastAdmin(device) ? (
                  <span className="devices-meta devices-last">only admin</span>
                ) : (
                  <button className="btn danger press" type="button" onClick={() => void revoke(device)}>
                    {device.current ? "Log out" : "Revoke"}
                  </button>
                )}
              </li>
            ))}
          </ul>
        ))}
    </section>
  );
}
