import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { apiJson, errorText, paths, type Kinematics, type MotorStatus, type Volume } from "../api.ts";
import { CONTROL_HINT_OFFLINE, clampVolume, kinematicsSwitch, kinematicsView } from "../logic/controls.ts";

export interface ControlHint {
  text: string;
  error: boolean;
}

interface Props {
  daemonOnline: boolean;
  controlBusy: boolean;
  setControlBusy: (busy: boolean) => void;
  status: string;
  setStatus: (text: string) => void;
  hint: ControlHint;
  setHint: (hint: ControlHint | ((prev: ControlHint) => ControlHint)) => void;
  robotControlMode: string | null;
  setRobotControlMode: (mode: string | null) => void;
  kinematics: Kinematics | null;
  setKinematics: (kin: Kinematics | null) => void;
  reloadKinematics: () => Promise<void>;
  appActive: boolean;
  /** Power-off bookkeeping for the kinematics switch (it restarts the robot). */
  setStopping: (stopping: boolean) => void;
  pollPower: () => Promise<void>;
  /** Motor re-enable starts a backend return-home session (#67). */
  onReturningHome: () => void;
}

type SliderId = "vol-spk" | "vol-mic";

const SLIDERS: { id: SliderId; label: string; status: string; current: string; set: string; initial: number }[] = [
  { id: "vol-spk", label: "Speaker volume", status: "speaker", current: paths.volumeCurrent, set: paths.volumeSet, initial: 80 },
  { id: "vol-mic", label: "Microphone volume", status: "microphone", current: paths.micCurrent, set: paths.micSet, initial: 60 },
];
const SAVE_DEBOUNCE_MS = 240;
const MOTOR_MODES = [
  { mode: "enabled", label: "Enabled" },
  { mode: "disabled", label: "Disabled" },
] as const;

export function ControlsPanel(props: Props) {
  const { daemonOnline, controlBusy, setControlBusy, setStatus, setHint, robotControlMode, setRobotControlMode } = props;
  const controlsDisabled = !(daemonOnline && !controlBusy);
  const onlineRef = useRef(daemonOnline);
  onlineRef.current = daemonOnline;

  // ---- volume sliders (#38: never fight an in-progress drag) ----
  const [values, setValues] = useState<Record<SliderId, number>>({ "vol-spk": 80, "vol-mic": 60 });
  const valuesRef = useRef(values);
  const [sliderActive, setSliderActive] = useState<SliderId | null>(null);
  const activeRef = useRef<SliderId | null>(null);
  const inputs = useRef<Partial<Record<SliderId, HTMLInputElement | null>>>({});
  const frozenDisabled = useRef<Partial<Record<SliderId, boolean>>>({});
  const saveSlots = useRef<Record<SliderId, { inflight: boolean; dirty: boolean; timer: number }>>({
    "vol-spk": { inflight: false, dirty: false, timer: 0 },
    "vol-mic": { inflight: false, dirty: false, timer: 0 },
  });

  const setValue = (id: SliderId, value: number) => {
    valuesRef.current = { ...valuesRef.current, [id]: value };
    setValues(valuesRef.current);
  };

  const syncSlider = (id: SliderId, value: unknown) => {
    if (value == null) return;
    // On touch a range input never gets focus, so check the drag flag too.
    if (activeRef.current === id || document.activeElement === inputs.current[id]) return;
    setValue(id, clampVolume(value));
  };

  const saveVolume = useCallback(
    async (id: SliderId) => {
      if (!onlineRef.current) return;
      const slot = saveSlots.current[id];
      const spec = SLIDERS.find((s) => s.id === id)!;
      if (slot.inflight) {
        // Coalesce: re-send the freshest value once this request returns.
        slot.dirty = true;
        return;
      }
      slot.inflight = true;
      setStatus(spec.status);
      try {
        await apiJson(spec.set, { method: "POST", body: JSON.stringify({ volume: valuesRef.current[id] }) });
        setStatus("ok");
        setHint({ text: "", error: false });
      } catch (err) {
        setStatus("error");
        setHint({ text: errorText(err, "Volume request failed"), error: true });
      } finally {
        slot.inflight = false;
        if (slot.dirty) {
          slot.dirty = false;
          void saveVolume(id);
        }
      }
    },
    [setStatus, setHint],
  );

  const scheduleSave = useCallback(
    (id: SliderId) => {
      const slot = saveSlots.current[id];
      window.clearTimeout(slot.timer);
      slot.timer = window.setTimeout(() => void saveVolume(id), SAVE_DEBOUNCE_MS);
    },
    [saveVolume],
  );

  // End of a drag: clear the guard (re-enabling the frozen disabled state) and
  // persist the released value. Pointer release can land off the thumb, so the
  // window listens too; a stuck flag would freeze daemon→slider sync.
  const endInteraction = useCallback(() => {
    const id = activeRef.current;
    if (id === null) return;
    activeRef.current = null;
    setSliderActive(null);
    scheduleSave(id);
  }, [scheduleSave]);

  useEffect(() => {
    window.addEventListener("pointerup", endInteraction);
    window.addEventListener("pointercancel", endInteraction);
    const elements = Object.values(inputs.current).filter((el): el is HTMLInputElement => Boolean(el));
    for (const el of elements) el.addEventListener("change", endInteraction); // native change = release
    return () => {
      window.removeEventListener("pointerup", endInteraction);
      window.removeEventListener("pointercancel", endInteraction);
      for (const el of elements) el.removeEventListener("change", endInteraction);
    };
  }, [endInteraction]);

  useEffect(
    () => () => {
      for (const slot of Object.values(saveSlots.current)) window.clearTimeout(slot.timer);
    },
    [],
  );

  const sliderDisabled = (id: SliderId) => {
    // Flipping .disabled mid-drag aborts an iOS touch drag: hold it until release.
    if (sliderActive === id && frozenDisabled.current[id] !== undefined) return frozenDisabled.current[id]!;
    frozenDisabled.current[id] = controlsDisabled;
    return controlsDisabled;
  };

  // ---- discrete controls ----
  const [kinSwitching, setKinSwitching] = useState<string | null>(null);
  const [gravityPending, setGravityPending] = useState<boolean | null>(null);
  const [wobble, setWobble] = useState(false);
  const [wobbleHint, setWobbleHint] = useState(false);
  const [testLabel, setTestLabel] = useState("Play test sound");

  const performControl = async <T,>(label: string, operation: () => Promise<T>): Promise<T | null> => {
    if (!onlineRef.current) {
      setStatus("offline");
      setHint((prev) => ({ ...prev, text: CONTROL_HINT_OFFLINE }));
      return null;
    }
    setControlBusy(true);
    setStatus(label);
    try {
      const result = await operation();
      setStatus("ok");
      setHint({ text: "", error: false });
      return result;
    } catch (err) {
      setStatus("error");
      setHint({ text: errorText(err, "Control request failed"), error: true });
      return null;
    } finally {
      setControlBusy(false);
    }
  };

  // Online flip: load the daemon's current values once. Offline: say why.
  const { reloadKinematics } = props;
  useEffect(() => {
    if (!daemonOnline) {
      setStatus("offline");
      setHint({ text: CONTROL_HINT_OFFLINE, error: false });
      return;
    }
    setStatus("online");
    setHint({ text: "", error: false });
    let cancelled = false;
    void (async () => {
      const [speaker, microphone, motors] = await Promise.allSettled([
        apiJson<Volume>(paths.volumeCurrent),
        apiJson<Volume>(paths.micCurrent),
        apiJson<MotorStatus>(paths.motorStatus),
      ]);
      if (cancelled) return;
      if (speaker.status === "fulfilled") syncSlider("vol-spk", speaker.value?.volume);
      if (microphone.status === "fulfilled") syncSlider("vol-mic", microphone.value?.volume);
      if (motors.status === "fulfilled") {
        const mode = motors.value?.mode;
        setRobotControlMode(typeof mode === "string" ? mode : null);
      }
      await reloadKinematics();
    })();
    return () => {
      cancelled = true;
    };
    // syncSlider only touches refs and stable setters.
  }, [daemonOnline, reloadKinematics, setHint, setRobotControlMode, setStatus]);

  const gravityChecked = gravityPending ?? robotControlMode === "gravity_compensation";
  const kin = kinematicsView(props.kinematics, controlsDisabled, props.appActive, gravityChecked);

  const onMotor = (mode: "enabled" | "disabled") => {
    if (mode === "disabled" && !window.confirm("Disable motors? The robot will go limp. Use Wake to recover.")) return;
    void performControl("motor", async () => {
      const result = await apiJson(paths.motorSetMode(mode), { method: "POST" });
      setRobotControlMode(mode);
      // The backend kicked a return-home session (#67); mirror it as a busy
      // expression so chips stay disabled without an enabled flash.
      if (mode === "enabled") props.onReturningHome();
      return result;
    });
  };

  const onKinematics = (engine: string, label: string) => {
    const current = props.kinematics;
    if (!current) return;
    const { needed, robotOn } = kinematicsSwitch(current, engine);
    if (!needed) return;
    if (robotOn && !window.confirm(`Switch to ${label} kinematics? Reachy will power off and back on with the new engine.`)) {
      return;
    }
    void performControl("kinematics", async () => {
      setKinSwitching(robotOn ? "Restarting Reachy on the new engine…" : "Switching…");
      if (robotOn) props.setStopping(true);
      try {
        props.setKinematics(
          await apiJson<Kinematics>(paths.kinematics, { method: "PUT", body: JSON.stringify({ engine }) }),
        );
      } finally {
        setKinSwitching(null);
        props.setStopping(false);
        void props.pollPower();
        void reloadKinematics();
      }
    });
  };

  const onGravity = (checked: boolean) => {
    const mode = checked ? "gravity_compensation" : "enabled";
    setGravityPending(checked);
    void performControl("motor", async () => {
      const result = await apiJson(paths.motorSetMode(mode), { method: "POST" });
      setRobotControlMode(mode);
      return result;
    }).finally(() => setGravityPending(null));
  };

  const onWobble = (checked: boolean) => {
    setWobble(checked);
    void performControl("wobble", () => apiJson(paths.wobble(checked), { method: "POST" })).then((result) => {
      if (result == null) setWobble(!checked);
      setWobbleHint(checked && result != null);
    });
  };

  const onTestSound = () => {
    setTestLabel("Playing…");
    void performControl("test sound", () => apiJson(paths.testSound, { method: "POST" })).finally(() =>
      setTestLabel("Play test sound"),
    );
  };

  const kinHint = kinSwitching ?? kin.hint;

  return (
    <section className="glass pad instrument">
      <div className="head">
        <h2>controls</h2>
        <div className="rule"></div>
        <span className="pill" id="control-status">
          {props.status}
        </span>
      </div>
      <p className={`panel-hint${props.hint.error ? " error" : ""}`} id="control-hint">
        {props.hint.text}
      </p>
      {SLIDERS.map((s) => (
        <div className="control" key={s.id}>
          <label htmlFor={s.id}>{s.label}</label>
          <div className="with-val">
            <input
              type="range"
              id={s.id}
              ref={(el) => {
                inputs.current[s.id] = el;
              }}
              min="0"
              max="100"
              value={values[s.id]}
              disabled={sliderDisabled(s.id)}
              style={{ "--fill": `${values[s.id]}%` } as CSSProperties}
              onPointerDown={() => {
                activeRef.current = s.id;
                setSliderActive(s.id);
              }}
              onChange={(e) => {
                activeRef.current = s.id;
                setSliderActive(s.id);
                setValue(s.id, Number(e.target.value));
                scheduleSave(s.id);
              }}
              onBlur={endInteraction}
            />
            <span className="ctl-val" id={`${s.id}-v`}>
              {values[s.id]}
            </span>
          </div>
        </div>
      ))}
      <div className="control">
        <label>Motors</label>
        <div className="segmented" id="motor-seg">
          {MOTOR_MODES.map(({ mode, label }) => (
            <button
              key={mode}
              type="button"
              data-mode={mode}
              className={robotControlMode === mode ? "active" : undefined}
              disabled={controlsDisabled}
              onClick={() => onMotor(mode)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="control">
        <label>Kinematics</label>
        <div className="segmented" id="kin-seg">
          {kin.buttons.map((b) => (
            <button
              key={b.engine}
              type="button"
              data-engine={b.engine}
              className={b.active ? "active" : undefined}
              disabled={b.disabled}
              title={b.title}
              onClick={() => onKinematics(b.engine, b.label)}
            >
              {b.label}
            </button>
          ))}
        </div>
      </div>
      <div className="control toggle-row">
        <label htmlFor="gravity">Gravity compensation</label>
        <label className="switch">
          <input
            type="checkbox"
            id="gravity"
            checked={gravityChecked}
            disabled={kin.gravityDisabled}
            onChange={(e) => onGravity(e.target.checked)}
          />
          <span className="slider"></span>
        </label>
      </div>
      <p className="panel-hint" id="kin-hint" hidden={kinHint === ""}>
        {kinHint}
      </p>
      <div className="control toggle-row">
        <label htmlFor="wobble">Audio-reactive wobble</label>
        <label className="switch">
          <input
            type="checkbox"
            id="wobble"
            checked={wobble}
            disabled={controlsDisabled}
            onChange={(e) => onWobble(e.target.checked)}
          />
          <span className="slider"></span>
        </label>
      </div>
      <p className="panel-hint" id="wobble-hint" hidden={!wobbleHint}>
        Reacts to audio — press <b>Play test sound</b> (or run an app) to see the head move.
      </p>
      <div className="control action">
        <button className="btn wide" id="testsound" type="button" disabled={controlsDisabled} onClick={onTestSound}>
          {testLabel}
        </button>
      </div>
    </section>
  );
}
