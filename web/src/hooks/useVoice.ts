import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson, errorText, paths, type VoiceOffer, type VoiceRoute, type VoiceView } from "../api.ts";
import { ICE_WAIT_MS, VOICE_POLL_MS } from "../logic/voice.ts";

export type VoiceStatus = "off" | "connecting" | "live";

export interface VoiceApi {
  view: VoiceView | null;
  /** The id of the session this browser opened, or null. */
  mine: string | null;
  status: VoiceStatus;
  /** What this browser asked for; meaningful while a session is open. */
  route: VoiceRoute;
  error: string | null;
  busy: boolean;
  start: (route: VoiceRoute) => Promise<void>;
  /** Switch listening or talking without reconnecting. */
  setRoute: (route: VoiceRoute) => Promise<void>;
  stop: () => Promise<void>;
}

const OFF: VoiceRoute = { listen: false, talk: false };

async function microphone(): Promise<MediaStreamTrack> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  return stream.getAudioTracks()[0];
}

/** Resolve once the browser has gathered its candidates (the gateway doesn't trickle). */
function gathered(pc: RTCPeerConnection): Promise<void> {
  return new Promise((resolve) => {
    if (pc.iceGatheringState === "complete") return resolve();
    const done = () => {
      if (pc.iceGatheringState === "complete") resolve();
    };
    pc.addEventListener("icegatheringstatechange", done);
    setTimeout(resolve, ICE_WAIT_MS);
  });
}

/**
 * Listen and talk (#219): one WebRTC session with the robot's audio gateway,
 * through the controller. The session outlives a hidden page (a phone with
 * its screen off keeps talking); polling stops while hidden and, back in
 * view, notices a session the controller ended (control or login lost).
 */
export function useVoice(visible: boolean): VoiceApi {
  const [view, setView] = useState<VoiceView | null>(null);
  const [status, setStatus] = useState<VoiceStatus>("off");
  const [route, setRouteState] = useState<VoiceRoute>(OFF);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [mine, setMine] = useState<string | null>(null);
  const pc = useRef<RTCPeerConnection | null>(null);
  const id = useRef<string | null>(null);
  // When `id` was set: a poll sent before then can't know the session.
  const openedAt = useRef(0);
  const mic = useRef<MediaStreamTrack | null>(null);
  const speaker = useRef<HTMLAudioElement | null>(null);

  const teardown = useCallback((why: string | null = null) => {
    pc.current?.close();
    pc.current = null;
    mic.current?.stop();
    mic.current = null;
    if (speaker.current) speaker.current.srcObject = null;
    id.current = null;
    setMine(null);
    setStatus("off");
    setRouteState(OFF);
    if (why) setError(why);
  }, []);

  const reload = useCallback(async () => {
    const sent = performance.now();
    try {
      const next = await apiJson<VoiceView>(paths.voice);
      setView(next);
      if (id.current && sent > openedAt.current && next.session?.id !== id.current) {
        teardown("Your audio session ended: this browser no longer has control of the robot.");
      }
    } catch {
      // An older controller has no audio routes; a blip retries next tick.
    }
  }, [teardown]);

  useEffect(() => {
    if (!visible) return;
    void reload();
    const timer = setInterval(() => void reload(), VOICE_POLL_MS);
    return () => clearInterval(timer);
  }, [visible, reload]);

  // Leaving the page ends the session; the gateway also drops one whose browser vanished.
  useEffect(() => () => teardown(), [teardown]);

  const run = useCallback(
    async (work: () => Promise<void>) => {
      setBusy(true);
      setError(null);
      try {
        await work();
      } finally {
        setBusy(false);
        await reload();
      }
    },
    [reload],
  );

  const start = useCallback(
    (next: VoiceRoute) =>
      run(async () => {
        teardown();
        let opened: string | null = null;
        try {
          // Ask for the mic first: a refusal shouldn't open the robot's.
          const track = next.talk ? await microphone() : null;
          mic.current = track;
          const { id: sid, offer } = await apiJson<VoiceOffer>(paths.voiceSession, { method: "POST", body: JSON.stringify(next) });
          opened = sid;
          id.current = sid;
          setMine(sid);
          openedAt.current = performance.now();
          setStatus("connecting");
          setRouteState(next);
          const peer = new RTCPeerConnection();
          pc.current = peer;
          speaker.current ??= new Audio();
          const out = speaker.current;
          out.autoplay = true;
          out.muted = !next.listen;
          peer.ontrack = (event) => {
            out.srcObject = event.streams[0] ?? new MediaStream([event.track]);
            void out.play().catch(() => {});
          };
          peer.onconnectionstatechange = () => {
            if (pc.current !== peer) return;
            if (peer.connectionState === "connected") setStatus("live");
            else if (peer.connectionState === "failed") teardown("The audio connection dropped.");
          };
          await peer.setRemoteDescription({ type: "offer", sdp: offer });
          // Always sendrecv, so talking can start later without renegotiating.
          const transceiver = peer.getTransceivers()[0];
          transceiver.direction = "sendrecv";
          if (track) await transceiver.sender.replaceTrack(track);
          await peer.setLocalDescription(await peer.createAnswer());
          await gathered(peer);
          await apiJson(paths.voiceAnswer(sid), { method: "POST", body: JSON.stringify({ sdp: peer.localDescription?.sdp }) });
        } catch (e) {
          teardown();
          if (opened) void apiJson(paths.voiceEnd(opened), { method: "DELETE" }).catch(() => {});
          setError(errorText(e, "Couldn't start the audio."));
        }
      }),
    [run, teardown],
  );

  const setRoute = useCallback(
    (next: VoiceRoute) =>
      run(async () => {
        const sid = id.current;
        const sender = pc.current?.getTransceivers()[0]?.sender;
        if (!sid || !sender) return;
        try {
          if (next.talk && !mic.current) {
            mic.current = await microphone();
            await sender.replaceTrack(mic.current);
          } else if (!next.talk && mic.current) {
            // Release the mic, so the browser's own mic indicator goes off too.
            await sender.replaceTrack(null);
            mic.current.stop();
            mic.current = null;
          }
          if (speaker.current) speaker.current.muted = !next.listen;
          await apiJson(paths.voiceRoute(sid), { method: "POST", body: JSON.stringify(next) });
          setRouteState(next);
        } catch (e) {
          setError(errorText(e, "Couldn't switch the audio."));
        }
      }),
    [run],
  );

  const stop = useCallback(
    () =>
      run(async () => {
        const sid = id.current;
        teardown();
        if (sid) await apiJson(paths.voiceEnd(sid), { method: "DELETE" }).catch(() => {});
      }),
    [run, teardown],
  );

  return { view, mine, status, route, error, busy, start, setRoute, stop };
}
