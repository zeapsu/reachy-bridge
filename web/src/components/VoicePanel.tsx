import { useState } from "react";
import type { VoiceApi } from "../hooks/useVoice.ts";
import type { ControlState } from "../logic/control.ts";
import { isMine, talkBlocked, voiceBadge, voiceBlocked } from "../logic/voice.ts";
import "./VoicePanel.css";

const secure = typeof window !== "undefined" && window.isSecureContext;
const hasMic = typeof navigator !== "undefined" && Boolean(navigator.mediaDevices?.getUserMedia);

/**
 * Listen and talk (#219), under the control bar: hear the robot's mic on
 * this device, and speak through its speaker. Either can be switched while
 * connected.
 */
export function VoicePanel({ voice, control }: { voice: VoiceApi; control: ControlState }) {
  const [wanted, setWanted] = useState({ listen: true, talk: false });
  const open = voice.status !== "off";
  const blocked = voiceBlocked(voice.view, control, secure);
  const noTalk = talkBlocked(secure, hasMic);
  const route = open ? voice.route : wanted;
  const session = voice.view?.session;
  const other = session && !isMine(session, voice.mine) ? voiceBadge(voice.view, voice.mine) : null;

  const flip = (key: "listen" | "talk") => {
    const next = { ...route, [key]: !route[key] };
    if (open) void voice.setRoute(next);
    else setWanted(next);
  };

  return (
    <div className={`voice${open ? " open" : ""}`} id="voice-panel" aria-labelledby="voice-title">
      <div className="voice-head">
        <h3 id="voice-title">Listen and talk</h3>
        <span className="chip">
          <span className={`dot${voice.status === "live" ? " ok" : voice.status === "connecting" ? " loading" : ""}`}></span>
          {voice.status === "live" ? "live" : voice.status === "connecting" ? "connecting" : "off"}
        </span>
      </div>
      <div className="row">
        <label htmlFor="voice-listen">Hear the robot on this device</label>
        <button
          className="switch"
          type="button"
          role="switch"
          id="voice-listen"
          aria-checked={route.listen}
          disabled={voice.busy || (!open && blocked !== null)}
          onClick={() => flip("listen")}
        ></button>
      </div>
      <div className="row">
        <label htmlFor="voice-talk">Talk through the robot</label>
        <button
          className="switch"
          type="button"
          role="switch"
          id="voice-talk"
          aria-checked={route.talk && noTalk === null}
          disabled={voice.busy || noTalk !== null || (!open && blocked !== null)}
          aria-describedby={noTalk ? "voice-talk-note" : undefined}
          onClick={() => flip("talk")}
        ></button>
      </div>
      {noTalk ? (
        <p className="note" id="voice-talk-note">
          {noTalk}
        </p>
      ) : null}
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
          disabled={voice.busy || blocked !== null || !(wanted.listen || (wanted.talk && noTalk === null))}
          onClick={() => void voice.start({ listen: wanted.listen, talk: wanted.talk && noTalk === null })}
        >
          Start
        </button>
      )}
      {!open && blocked ? <p className="note">{blocked}</p> : null}
      {other ? <p className="note">{other}. Starting here ends theirs.</p> : null}
      {voice.error ? (
        <p className="note error" role="alert">
          {voice.error}
        </p>
      ) : null}
    </div>
  );
}
