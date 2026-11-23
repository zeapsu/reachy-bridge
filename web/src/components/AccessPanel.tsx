import { Fragment, useCallback, useEffect, useState, type ReactNode } from "react";
import { apiJson, errorText, paths } from "../api.ts";
import { accessList, isLastAdmin, makeAdminQuestion, waitingText, type WaitingDevice } from "../logic/access.ts";
import { normalizeCode, seenText, type PairedDevice, type ProfileItem } from "../logic/auth.ts";
import { ALL_CAN, appsText } from "../logic/profile.ts";
import { Panel } from "./Panel.tsx";
import { Avatar, ProfileEditor, type InstalledApp } from "./Profiles.tsx";
import "./AccessPanel.css";

const stroke = { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true } as const;
const ICONS = {
  device: (
    <svg {...stroke}>
      <rect x="4" y="5" width="16" height="11" rx="2" />
      <path d="M2 20h20" />
    </svg>
  ),
  tablet: (
    <svg {...stroke}>
      <rect x="4.5" y="2.5" width="15" height="19" rx="2.5" />
      <path d="M11 18.5h2" />
    </svg>
  ),
  shield: (
    <svg {...stroke} strokeWidth={2.2}>
      <path d="M12 3 5 6v5.5c0 4.5 3 7.8 7 9.5 4-1.7 7-5 7-9.5V6l-7-3Z" />
    </svg>
  ),
  plus: (
    <svg {...stroke}>
      <path d="M12 5v14M5 12h14" />
    </svg>
  ),
};

const ADMIN_TAG = <span className="role-tag">{ICONS.shield}Admin</span>;

/** A profile that limits its person today: the one Make admin would lift. */
const limits = (p: ProfileItem) => !p.admin && (p.apps != null || Object.values(p.can ?? ALL_CAN).some((on) => !on));

interface Props {
  installed: InstalledApp[];
  /** The admin using this browser: an admin device approved here is theirs. */
  person: string | null;
  waiting: WaitingDevice[];
}

/**
 * Who may use the robot, for the admin (#334): every profile with its
 * devices, shared tablets, and the browsers waiting for approval. A waiting
 * browser is approved by typing the code its own screen shows, then choosing
 * who it is for; the code never comes from the controller.
 */
export function AccessPanel({ installed, person, waiting }: Props) {
  const [profiles, setProfiles] = useState<ProfileItem[]>([]);
  const [devices, setDevices] = useState<PairedDevice[]>([]);
  const [editing, setEditing] = useState<ProfileItem | "new" | null>(null);
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState<{ text: string; error: boolean } | null>(null);

  const reload = useCallback(async () => {
    try {
      const [p, d] = await Promise.all([
        apiJson<{ profiles: ProfileItem[] }>(paths.authProfiles),
        apiJson<{ devices: PairedDevice[] }>(paths.authDevices),
      ]);
      setProfiles(p.profiles);
      setDevices(d.devices);
    } catch (err) {
      setHint({ text: errorText(err, "Couldn't load devices"), error: true });
    }
  }, []);

  // A browser that stops waiting was approved (it is a device now) or gave up.
  useEffect(() => {
    void reload();
  }, [reload, waiting.length]);

  if (editing) {
    return (
      <ProfileEditor
        key={editing === "new" ? "new" : editing.name}
        initial={editing === "new" ? null : editing}
        installed={installed}
        devices={devices}
        onDone={() => {
          setEditing(null);
          void reload();
        }}
        onChanged={reload}
      />
    );
  }

  const act = async (failed: string, action: () => Promise<void>) => {
    setBusy(true);
    setHint(null);
    try {
      await action();
    } catch (err) {
      setHint({ text: errorText(err, failed), error: true });
    } finally {
      setBusy(false);
    }
  };

  const typed = normalizeCode(code);
  const approve = (as: { role?: "admin"; person?: string; shared?: boolean }) =>
    act("Approval failed", async () => {
      await apiJson(paths.authApprove, { method: "POST", body: JSON.stringify({ code: typed, name: name.trim() || undefined, ...as }) });
      setCode("");
      setName("");
      setHint({ text: "Approved", error: false });
    });

  const revoke = (device: PairedDevice) => {
    const question = device.current ? `Log this browser (${device.name}) out? It will need a new code to log in.` : `Revoke ${device.name}?`;
    if (!window.confirm(question)) return;
    void act("Revoke failed", async () => {
      await apiJson(paths.authDevice(device.id), { method: "DELETE" });
      if (device.current) window.location.reload();
      else await reload();
    });
  };

  const makeAdmin = (device: PairedDevice, profile: ProfileItem) => {
    if (!window.confirm(makeAdminQuestion(device, profile.name, limits(profile)))) return;
    void act("Couldn't make it an admin", async () => {
      await apiJson(paths.authDeviceRole(device.id), { method: "PUT", body: JSON.stringify({ role: "admin" }) });
      await reload();
    });
  };

  const list = accessList(profiles, devices);
  const now = Date.now();
  const members = profiles.filter((p) => !p.admin);

  const deviceRow = (device: PairedDevice, nested: boolean, lead: ReactNode) => (
    <li className={`acc-row${nested ? " nested" : ""}`} key={device.id}>
      <span className={`acc-ico${nested ? "" : " round"}`}>{device.shared ? ICONS.tablet : ICONS.device}</span>
      <span className="acc-name">
        {device.name}
        <span className="acc-meta">
          {device.current ? "This browser" : seenText(device.last_seen, now)}
          <span className="peer"> · {device.peer}</span>
        </span>
      </span>
      <span className="acc-trail">
        {lead}
        {/* The controller keeps the last admin device: no button that it would refuse. */}
        {isLastAdmin(devices, device) ? null : (
          <button className="btn danger press" type="button" disabled={busy} onClick={() => revoke(device)}>
            {device.current ? "Log out" : "Revoke"}
          </button>
        )}
      </span>
    </li>
  );

  const ownRow = (device: PairedDevice, profile: ProfileItem) =>
    deviceRow(
      device,
      !list.solo,
      device.role === "admin" ? (
        list.solo ? ADMIN_TAG : null
      ) : (
        <button className="btn press" type="button" disabled={busy} onClick={() => makeAdmin(device, profile)}>
          {ICONS.shield}Make admin
        </button>
      ),
    );

  return (
    <>
      {waiting.length > 0 ? (
        <form className="acc-waiting" aria-label="Waiting for approval" onSubmit={(event) => event.preventDefault()}>
          <ul>
            {waiting.map((w, index) => (
              <li key={index}>
                <span className="set-ico">{ICONS.device}</span>
                <span className="acc-name">
                  {w.peer}
                  <span className="acc-meta">{waitingText(w.age)}</span>
                </span>
              </li>
            ))}
          </ul>
          <div className="acc-fields">
            <input
              className="field acc-code"
              aria-label="Code on the waiting device"
              inputMode="numeric"
              autoComplete="off"
              placeholder="123 456"
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
            <input className="field" aria-label="Device name (optional)" placeholder="name" maxLength={64} value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="acc-actions">
            <button className="btn primary press" type="button" disabled={!typed || busy} onClick={() => void approve({ role: "admin", person: person ?? undefined })}>
              {ICONS.shield}Approve as Admin
            </button>
            <span className="or">or for</span>
            {members.map((p) => (
              <button className="btn press who" type="button" key={p.name} disabled={!typed || busy} onClick={() => void approve({ person: p.name })}>
                <Avatar name={p.name} size={24} />
                {p.name}
              </button>
            ))}
            {members.length === 0 ? (
              <button className="btn press" type="button" onClick={() => setEditing("new")}>
                {ICONS.plus}New profile
              </button>
            ) : null}
            <button className="btn press who" type="button" disabled={!typed || busy} onClick={() => void approve({ shared: true })}>
              <span className="acc-ico">{ICONS.tablet}</span>
              Shared
            </button>
          </div>
        </form>
      ) : null}
      <Panel inset title={list.solo ? "Devices" : "Profiles"} titleId="access-title" id="access-panel">
        <ul className="acc-list">
          {list.people.map(({ profile, devices: own }) => (
            <Fragment key={profile.name}>
              {list.solo ? null : (
                <li className="acc-row">
                  <Avatar name={profile.name} size={40} />
                  <span className="acc-name">
                    {profile.name}
                    {profile.admin ? null : <span className="acc-meta">{appsText(profile.apps)}</span>}
                  </span>
                  <span className="acc-trail">
                    {profile.admin ? (
                      ADMIN_TAG
                    ) : (
                      <button className="btn press" type="button" aria-label={`Edit ${profile.name}`} onClick={() => setEditing({ ...profile, can: profile.can ?? ALL_CAN })}>
                        Edit
                      </button>
                    )}
                  </span>
                </li>
              )}
              {own.map((device) => ownRow(device, profile))}
            </Fragment>
          ))}
          {list.shared.map((device) => deviceRow(device, false, <span className="role-tag plain">Shared</span>))}
          <li className="acc-row">
            <button className="acc-add press" type="button" onClick={() => setEditing("new")}>
              <span className="plus">{ICONS.plus}</span>
              Add profile
            </button>
          </li>
        </ul>
        {hint ? (
          <p className={`note ${hint.error ? "error" : "ok"}`} role={hint.error ? "alert" : "status"}>
            {hint.text}
          </p>
        ) : null}
      </Panel>
    </>
  );
}
