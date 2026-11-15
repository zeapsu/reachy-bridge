import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { apiJson, errorText, paths } from "../api.ts";
import { useLens } from "../glass.ts";
import { appHue, appInitial } from "../logic/apps.ts";
import type { Can, PairedDevice, ProfileItem, ProfileView } from "../logic/auth.ts";
import { ALL_CAN, NEW_CAN, appsText, profileHue, profileInitial } from "../logic/profile.ts";
import "./Profiles.css";

const stroke = { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true } as const;

const LOCK = (
  <svg {...stroke}>
    <rect x="5" y="11" width="14" height="10" rx="2.5" />
    <path d="M8 11V8a4 4 0 0 1 8 0v3" />
  </svg>
);

const CAN_ROWS: { key: keyof Can; label: string; icon: ReactNode }[] = [
  {
    key: "power",
    label: "Power on and off",
    icon: (
      <svg {...stroke}>
        <path d="M12 3v8" />
        <path d="M6.3 7.2a8 8 0 1 0 11.4 0" />
      </svg>
    ),
  },
  {
    key: "drive",
    label: "Drive and expressions",
    icon: (
      <svg {...stroke}>
        <circle cx="12" cy="12" r="8" />
        <circle cx="12" cy="12" r="2.5" />
      </svg>
    ),
  },
  {
    key: "voice",
    label: "Talk and listen",
    icon: (
      <svg {...stroke}>
        <rect x="9" y="3" width="6" height="11" rx="3" />
        <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
      </svg>
    ),
  },
  {
    key: "camera",
    label: "Camera",
    icon: (
      <svg {...stroke}>
        <path d="M4 8h3l2-2.5h6L17 8h3v11H4z" />
        <circle cx="12" cy="13" r="3.5" />
      </svg>
    ),
  },
  {
    key: "controls",
    label: "Volume, sounds and motors",
    icon: (
      <svg {...stroke}>
        <path d="M4 7h9M17 7h3M4 17h3M11 17h9" />
        <circle cx="15" cy="7" r="2" />
        <circle cx="9" cy="17" r="2" />
      </svg>
    ),
  },
];

export function Avatar({ name, size = 26 }: { name: string | null; size?: number }) {
  const style = { "--hue": name ? profileHue(name) : 220, width: size, height: size, fontSize: size * 0.46 } as CSSProperties;
  return (
    <span className={`avatar${name ? "" : " empty"}`} style={style} aria-hidden="true">
      {name ? profileInitial(name) : "?"}
    </span>
  );
}

/** The profile chip in the top bar: who this browser is (#297). */
export function ProfileChip({ profile, onOpen }: { profile: ProfileView; onOpen: () => void }) {
  const name = profile.name ?? "Pick";
  return (
    <button className="glass profile-chip press" type="button" aria-label={`Profile: ${name}`} title={name} onClick={onOpen}>
      <Avatar name={profile.name} size={24} />
      <span className="profile-chip-name">{name}</span>
    </button>
  );
}

/**
 * A full-page layer over the dashboard: the shared device's picker and the
 * profile sheet.
 */
function Layer({ children, onClose, label }: { children: ReactNode; onClose?: () => void; label: string }) {
  useEffect(() => {
    if (!onClose) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div
      className="profile-layer"
      role="dialog"
      aria-modal="true"
      aria-label={label}
      onClick={(event) => {
        if (onClose && event.target === event.currentTarget) onClose();
      }}
    >
      {children}
    </div>
  );
}

/**
 * Who is using this shared device (#297). Admins are shown, dimmed with a
 * lock: their profile is only used on their own devices.
 */
export function ProfilePicker({ onPick, error }: { onPick: (person: string) => Promise<boolean>; error: string | null }) {
  const sheet = useLens<HTMLDivElement>();
  const [people, setPeople] = useState<ProfileItem[] | null>(null);
  const [busy, setBusy] = useState(false);
  // Re-read now and then: a failed load retries, and new profiles appear.
  useEffect(() => {
    const load = () =>
      void apiJson<{ profiles: ProfileItem[] }>(paths.authProfiles)
        .then((body) => setPeople(body.profiles))
        .catch(() => undefined);
    load();
    const timer = window.setInterval(load, 5000);
    return () => window.clearInterval(timer);
  }, []);
  return (
    <Layer label="Pick a profile">
      <div className="glass sheet picker-card" data-lens ref={sheet}>
        <div className="brand picker-brand">reachy</div>
        {people === null ? <span className="spin" aria-hidden="true"></span> : null}
        <ul className="picker-grid">
          {(people ?? []).map((p) => (
            <li key={p.name}>
              <button
                className={`press pick${p.admin ? " locked" : ""}`}
                type="button"
                disabled={p.admin || busy}
                title={p.admin ? `${p.name}: admin` : p.name}
                onClick={async () => {
                  setBusy(true);
                  await onPick(p.name);
                  setBusy(false);
                }}
              >
                <span className="pick-avatar">
                  <Avatar name={p.name} size={84} />
                  {p.admin ? <span className="pick-lock">{LOCK}</span> : null}
                </span>
                <span className="pick-name">{p.name}</span>
              </button>
            </li>
          ))}
        </ul>
        {people && people.length === 0 ? <p className="note">No profiles yet</p> : null}
        <p className="note error" role="alert">
          {error}
        </p>
      </div>
    </Layer>
  );
}

/**
 * The chip's sheet: this browser's look, and handing a shared device back
 * or logging this one out (#297).
 */
export function ProfileSheet({
  profile,
  onClose,
  onSwitch,
  children,
}: {
  profile: ProfileView;
  onClose: () => void;
  onSwitch: () => void;
  children: ReactNode;
}) {
  const sheet = useLens<HTMLElement>();
  return (
    <Layer label="Profile" onClose={onClose}>
      <div className="profile-sheet-stack">
        <section className="glass sheet" data-lens ref={sheet} aria-label={profile.name ?? "Profile"}>
          <header className="profile-sheet-head">
            <Avatar name={profile.name} size={44} />
            <span className="profile-sheet-name">
              {profile.name ?? "Shared"}
              <span className="profile-sheet-kind">{profile.admin ? "Admin" : appsText(profile.apps)}</span>
            </span>
            {profile.shared ? (
              <button className="btn press" type="button" onClick={onSwitch}>
                Switch
              </button>
            ) : null}
            <button className="btn quiet press" type="button" onClick={onClose}>
              Done
            </button>
          </header>
        </section>
        {children}
      </div>
    </Layer>
  );
}

/** An installed app: its name and the name it shows. */
export interface InstalledApp {
  name: string;
  display: string;
}

interface EditorProps {
  initial: ProfileItem | null;
  installed: InstalledApp[];
  devices: PairedDevice[];
  onDone: () => void;
  onChanged: () => Promise<void>;
}

/** The admin's editor for one profile; changes save as they're made. */
function ProfileEditor({ initial, installed, devices, onDone, onChanged }: EditorProps) {
  const sheet = useLens<HTMLElement>();
  const [saved, setSaved] = useState<string | null>(initial?.name ?? null);
  const [name, setName] = useState(initial?.name ?? "");
  const [can, setCan] = useState<Can>(initial?.can ?? NEW_CAN);
  const [apps, setApps] = useState<string[] | null>(initial ? (initial.apps ?? null) : []);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const own = devices.filter((d) => initial?.devices?.includes(d.id));

  // Saves run one at a time, each sending the newest values to the name
  // the profile has on the robot at that moment (a rename changes it).
  const latest = useRef({ name, can, apps });
  latest.current = { ...latest.current, name, can, apps };
  const savedRef = useRef(saved);
  // What the robot holds, to put back when a save fails.
  const good = useRef({ name, can, apps });
  const queue = useRef(Promise.resolve());
  const pending = useRef(0);

  const save = (next: { can?: Can; apps?: string[] | null }) => {
    latest.current = { ...latest.current, ...next };
    if (!latest.current.name.trim()) {
      setError("A profile needs a name.");
      return;
    }
    pending.current += 1;
    setBusy(true);
    queue.current = queue.current.then(async () => {
      const { name: typed, can: c, apps: a } = latest.current;
      const body = { name: typed.trim(), can: c, apps: a, create: savedRef.current === null };
      try {
        await apiJson(paths.authProfile(savedRef.current ?? body.name), { method: "PUT", body: JSON.stringify(body) });
        savedRef.current = body.name;
        good.current = { name: body.name, can: c, apps: a };
        setSaved(body.name);
        setError(null);
        await onChanged();
      } catch (err) {
        setError(errorText(err, "Couldn't save the profile"));
        // Show what the robot kept, so the next change doesn't retry this one.
        if (savedRef.current !== null) {
          latest.current = { ...good.current };
          setName(good.current.name);
          setCan(good.current.can);
          setApps(good.current.apps);
        }
      } finally {
        pending.current -= 1;
        if (pending.current === 0) setBusy(false);
      }
    });
  };

  const remove = async () => {
    if (!saved || !window.confirm(`Remove ${saved}'s profile?`)) return;
    try {
      await apiJson(paths.authProfile(saved), { method: "DELETE" });
      await onChanged();
      onDone();
    } catch (err) {
      setError(errorText(err, "Couldn't remove the profile"));
    }
  };

  const logOut = async (device: PairedDevice) => {
    if (!window.confirm(`Log out ${device.name}?`)) return;
    try {
      await apiJson(paths.authDevice(device.id), { method: "DELETE" });
      await onChanged();
    } catch (err) {
      setError(errorText(err, "Couldn't log the device out"));
    }
  };

  const names = installed.map((app) => app.name);
  const toggleApp = (app: string, on: boolean) => {
    const current = apps ?? names;
    const next = on ? [...current.filter((a) => a !== app), app] : current.filter((a) => a !== app);
    setApps(next);
    void save({ apps: next });
  };

  return (
    <section className="glass sheet profile-editor" data-lens ref={sheet} aria-labelledby="profile-edit-title">
      <header>
        <h2 id="profile-edit-title">Profile</h2>
        {busy ? <span className="spin" aria-hidden="true"></span> : null}
        <button className="btn quiet press" type="button" onClick={onDone}>
          Done
        </button>
      </header>
      <form
        className="profile-edit-head"
        onSubmit={(event) => {
          event.preventDefault();
          void save({});
        }}
      >
        <Avatar name={name.trim() || null} size={56} />
        <input
          className="field"
          value={name}
          maxLength={64}
          placeholder="Name"
          aria-label="Name"
          autoFocus={!saved}
          onChange={(event) => setName(event.target.value)}
          onBlur={() => {
            if (saved && name.trim() && name.trim() !== saved) void save({});
          }}
        />
        {saved ? null : (
          <button className="btn primary press" type="submit" disabled={busy || !name.trim()}>
            Add
          </button>
        )}
      </form>
      <p className="note error" role="alert">
        {error}
      </p>
      {saved ? (
        <>
          <div className="group">
            <span className="label">Can</span>
            {CAN_ROWS.map((row) => (
              <div className="row profile-toggle" key={row.key}>
                <span className="profile-toggle-label">
                  {row.icon}
                  {row.label}
                </span>
                <button
                  className="switch"
                  type="button"
                  role="switch"
                  aria-label={row.label}
                  aria-checked={can[row.key]}
                  onClick={() => {
                    const next = { ...can, [row.key]: !can[row.key] };
                    setCan(next);
                    void save({ can: next });
                  }}
                ></button>
              </div>
            ))}
          </div>
          <div className="group">
            <span className="label">Apps · {apps === null ? "all" : `${apps.filter((a) => names.includes(a)).length} of ${names.length}`}</span>
            <div className="row profile-toggle">
              <span className="profile-toggle-label">All apps</span>
              <button
                className="switch"
                type="button"
                role="switch"
                aria-label="All apps"
                aria-checked={apps === null}
                onClick={() => {
                  const next = apps === null ? [] : null;
                  setApps(next);
                  void save({ apps: next });
                }}
              ></button>
            </div>
            {apps === null ? null : (
              <ul className="profile-apps">
                {installed.map(({ name: app, display }) => {
                  const on = apps.includes(app);
                  return (
                    <li key={app} className={on ? "" : "off"}>
                      <span className="app-tile" style={{ "--hue": appHue(app) } as CSSProperties} aria-hidden="true">
                        {appInitial(display)}
                      </span>
                      <span className="profile-app-name">{display}</span>
                      <button
                        className="switch"
                        type="button"
                        role="switch"
                        aria-label={display}
                        aria-checked={on}
                        onClick={() => toggleApp(app, !on)}
                      ></button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
          <div className="group">
            <span className="label">Devices</span>
            {own.map((device) => (
              <div className="row" key={device.id}>
                <span>{device.name}</span>
                <button className="btn danger press" type="button" onClick={() => void logOut(device)}>
                  Log out
                </button>
              </div>
            ))}
            {own.length === 0 ? (
              <button className="btn danger press profile-remove" type="button" onClick={() => void remove()}>
                Remove profile
              </button>
            ) : null}
          </div>
        </>
      ) : null}
    </section>
  );
}

/** Everyone's profile, for the admin in System (#297). */
export function ProfilesPanel({ installed }: { installed: InstalledApp[] }) {
  const sheet = useLens<HTMLElement>();
  const [profiles, setProfiles] = useState<ProfileItem[]>([]);
  const [devices, setDevices] = useState<PairedDevice[]>([]);
  const [editing, setEditing] = useState<ProfileItem | "new" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const [p, d] = await Promise.all([
        apiJson<{ profiles: ProfileItem[] }>(paths.authProfiles),
        apiJson<{ devices: PairedDevice[] }>(paths.authDevices),
      ]);
      setProfiles(p.profiles);
      setDevices(d.devices);
      setError(null);
    } catch (err) {
      setError(errorText(err, "Couldn't load profiles"));
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

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

  const deviceName = (id: string) => devices.find((d) => d.id === id)?.name ?? id;
  return (
    <section className="glass sheet" data-lens ref={sheet} aria-labelledby="profiles-title">
      <header>
        <h2 id="profiles-title">Profiles</h2>
        <button className="btn press" type="button" onClick={() => setEditing("new")}>
          Add
        </button>
      </header>
      <ul className="profiles-list">
        {profiles.map((p) => (
          <li key={p.name}>
            <Avatar name={p.name} size={40} />
            <span className="profiles-name">{p.name}</span>
            <span className="profiles-meta">
              {p.admin ? <span className="profile-kind kind-admin">Admin</span> : null}
              {[...(p.devices ?? []).map(deviceName), appsText(p.admin ? null : p.apps)].join(" · ")}
            </span>
            {p.admin ? null : (
              <button className="btn press" type="button" onClick={() => setEditing({ ...p, can: p.can ?? ALL_CAN })}>
                Edit
              </button>
            )}
          </li>
        ))}
      </ul>
      <p className="note error" role="alert">
        {error}
      </p>
    </section>
  );
}
