import { useEffect, useRef, useState } from "react";
import { useMicLevel } from "../hooks/useMicLevel.ts";
import type { VoiceApi } from "../hooks/useVoice.ts";
import type { ControlState } from "../logic/control.ts";
import { isMine, onTailnet, talkBlocked, voiceBlocked } from "../logic/voice.ts";
import { ROUTE_ICONS } from "./Shell.tsx";
import "./VoicePanel.css";

const secure = typeof window !== "undefined" && window.isSecureContext;
const tailnet = secure || (typeof location !== "undefined" && onTailnet(location.hostname));
const hasMic = typeof navigator !== "undefined" && Boolean(navigator.mediaDevices?.getUserMedia);

const MIC_OFF_ICON = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M15 9.3V6a3 3 0 0 0-5.7-1.3M9 9v2a3 3 0 0 0 5.1 2.1M5 11a7 7 0 0 0 11.6 5.3M19 11a7 7 0 0 1-.4 2.3M12 18v3M4 4l16 16" />
  </svg>
);

/** This browser's audio inputs, listed while `want` (names need mic permission, which talking has). */
function useInputs(want: boolean): MediaDeviceInfo[] {
  const [inputs, setInputs] = useState<MediaDeviceInfo[]>([]);
  useEffect(() => {
    const devices = navigator.mediaDevices;
    if (!want || !devices?.enumerateDevices) return;
    const load = () =>
      void devices
        .enumerateDevices()
        .then((all) => setInputs(all.filter((d) => d.kind === "audioinput" && d.deviceId !== "default" && d.deviceId !== "communications")))
        .catch(() => {});
    load();
    devices.addEventListener("devicechange", load);
    return () => devices.removeEventListener("devicechange", load);
  }, [want]);
  return inputs;
}

/**
 * Listen and talk (#219), the Robot tab's Voice pane: hear the robot's mic on
 * this device, hear what the robot says (its speaker goes quiet meanwhile),
 * speak through its speaker, and speak to its apps (into what they hear as
 * its mic). Each can be switched while connected.
 */
export function VoicePanel({ voice, control, robotOff }: { voice: VoiceApi; control: ControlState; robotOff: boolean }) {
  const [wanted, setWanted] = useState({ listen: true, talk: false, hear: false, apps: false });
  const open = voice.status !== "off";
  const blocked = voiceBlocked(voice.view, control, tailnet);
  const noTalk = talkBlocked(secure, hasMic);
  const route = open ? voice.route : wanted;
  const speaks = wanted.talk || wanted.apps;
  const session = voice.view?.session;
  const other = Boolean(session && !isMine(session, voice.mine));
  // The lit Talk chip fills with this browser's mic level, so a silent or
  // wrong mic shows as a chip that stays flat.
  const root = useRef<HTMLDivElement>(null);
  const sending = open && (route.talk || route.apps) ? voice.micTrack : null;
  const silent = useMicLevel(sending, root);
  const inputs = useInputs(silent);
  const current = sending?.getSettings().deviceId ?? "";

  const flip = (key: "listen" | "talk" | "hear" | "apps") => {
    const next = { ...route, [key]: !route[key] };
    if (open) void voice.setRoute(next);
    else setWanted(next);
  };

  const chip = (id: string, key: "listen" | "talk" | "hear" | "apps", label: string, name: string, off = false) => (
    <button
      className="voice-chip press"
      type="button"
      role="switch"
      id={id}
      aria-label={`${label}: ${name}`}
      aria-describedby={off ? "voice-talk-note" : undefined}
      title={name}
      aria-checked={route[key] && !off}
      disabled={voice.busy || off || (!open && (blocked !== null || robotOff))}
      onClick={() => flip(key)}
    >
      {ROUTE_ICONS[key]}
      {label}
    </button>
  );

  return (
    <div ref={root} className={`voice${open ? " open" : ""}${sending ? " sending" : ""}`} id="voice-panel" role="group" aria-label="Listen and talk">
      {open ? (
        <span className="chip voice-state">
          <span className={`dot${voice.status === "live" ? " ok" : " loading"}`}></span>
          {voice.status === "live" ? "live" : "connecting"}
        </span>
      ) : null}
      <div className="voice-group" role="group" aria-labelledby="voice-listen-label">
        <span className="label" id="voice-listen-label">Listen</span>
        <div className="voice-chips">
          {chip("voice-listen", "listen", "Surroundings", "hear Reachy's surroundings")}
          {chip("voice-hear-robot", "hear", "Reachy's voice", "hear what Reachy says")}
        </div>
      </div>
      <div className="voice-group" role="group" aria-labelledby="voice-talk-label">
        <span className="label" id="voice-talk-label">Talk</span>
        <div className="voice-chips">
          {chip("voice-talk", "talk", "Out loud", "talk through the robot", noTalk !== null)}
          {chip("voice-apps", "apps", "To apps", "talk to Reachy's apps", noTalk !== null)}
        </div>
        {/* Announced as it appears; the select says which mic is silent. */}
        <div className="mic-live" aria-live="polite">
          {silent && sending ? (
            <label className="mic-silent">
              <span className="sr-only">No sound from this microphone</span>
              {MIC_OFF_ICON}
              <select
                aria-label="Microphone"
                value={current}
                disabled={voice.busy}
                onChange={(e) => void voice.switchMic(e.target.value)}
              >
                {inputs.some((d) => d.deviceId === current) ? null : <option value={current}>{sending.label || "Microphone"}</option>}
                {inputs.map((d, i) => (
                  <option key={d.deviceId} value={d.deviceId}>
                    {d.label || `Microphone ${i + 1}`}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
        </div>
        {noTalk ? <p className="note" id="voice-talk-note">{noTalk}</p> : null}
      </div>
      {open && voice.needsTap ? (
        <button className="btn primary press" type="button" id="voice-hear" onClick={voice.hear}>
          Tap to hear the robot
        </button>
      ) : null}
      {open ? (
        <button className="btn press" type="button" id="voice-stop" disabled={voice.busy} onClick={() => void voice.stop()}>
          Stop
        </button>
      ) : (
        <button
          className="btn primary press"
          type="button"
          id="voice-start"
          disabled={voice.busy || blocked !== null || robotOff || !(wanted.listen || wanted.hear || (speaks && noTalk === null))}
          onClick={() => void voice.start({ ...wanted, talk: wanted.talk && noTalk === null, apps: wanted.apps && noTalk === null })}
        >
          {other ? "Take over audio" : "Start"}
        </button>
      )}
      {/* The control strip above already says who is in control. */}
      {!open && blocked && control.kind !== "other" ? <p className="note">{blocked}</p> : null}
      {voice.error ? (
        <p className="note error" role="alert">
          {voice.error}
        </p>
      ) : null}
    </div>
  );
}
