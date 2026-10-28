import { useCallback, useEffect, useState, type FormEvent } from "react";
import { apiJson, errorText, paths } from "../api.ts";
import { useLens } from "../glass.ts";
import { normalizeCode, seenText, type AuthStatus, type PairedDevice } from "../logic/auth.ts";
import "./DevicesPanel.css";

/**
 * Paired browsers for LAN access (#141): approve a login code, revoke a
 * device. Renders nothing while LAN access is off.
 */
export function DevicesPanel() {
  const [lan, setLan] = useState(false);
  const [devices, setDevices] = useState<PairedDevice[]>([]);
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [hint, setHint] = useState<{ text: string; error: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
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
        setLan(status.lan);
        if (status.lan) await reload();
      } catch {
        setLan(false);
      }
    })();
  }, [reload]);

  if (!lan) return null;

  const approve = async (event: FormEvent) => {
    event.preventDefault();
    const normalized = normalizeCode(code);
    if (!normalized) {
      setHint({ text: "The code is 6 digits.", error: true });
      return;
    }
    setBusy(true);
    try {
      await apiJson(paths.authApprove, { method: "POST", body: JSON.stringify({ code: normalized, name: name.trim() || undefined }) });
      setCode("");
      setName("");
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
      ? `Revoke this browser (${device.name})? It will need a new code to log in.`
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
  return (
    <section className="glass sheet devices-panel" data-lens ref={sheet} id="devices-panel" aria-labelledby="devices-title">
      <header>
        <h2 id="devices-title">Devices</h2>
      </header>
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
        <input className="field" aria-label="Device name (optional)" placeholder="name (optional)" maxLength={64} value={name} onChange={(e) => setName(e.target.value)} />
        <button className="btn primary press" type="submit" disabled={busy}>
          Approve
        </button>
      </form>
      {hint && <p className={`note ${hint.error ? "error" : "ok"}`}>{hint.text}</p>}
      {devices.length === 0 ? (
        <p className="note">No paired browsers.</p>
      ) : (
        <ul className="devices-list">
          {devices.map((device) => (
            <li key={device.id}>
              <span className="devices-name">
                {device.name}
                {device.current && <span className="devices-current"> · this browser</span>}
              </span>
              <span className="devices-meta">
                {seenText(device.last_seen, now)} · {device.peer}
              </span>
              <button className="btn danger press" type="button" onClick={() => void revoke(device)}>
                Revoke
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
