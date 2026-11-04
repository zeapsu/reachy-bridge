import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson, errorText, paths, takeControlIfFree, type VoiceOffer, type VoiceRoute, type VoiceView } from "../api.ts";
import { DISCONNECT_GRACE_MS, ICE_WAIT_MS, VOICE_POLL_MS } from "../logic/voice.ts";

export type VoiceStatus = "off" | "connecting" | "live";

export interface VoiceApi {
  view: VoiceView | null;
  /** The public ref of the session this browser opened, or null. */
  mine: string | null;
  /** The browser wouldn't start playing the robot's audio without a tap. */
  needsTap: boolean;
  /** Play the robot's audio (from a tap). */
  hear: () => void;
  status: VoiceStatus;
  /** What this browser asked for; meaningful while a session is open. */
  route: VoiceRoute;
  error: string | null;
  busy: boolean;
  /** This browser's mic while it's sending, for the level meter. */
  micTrack: MediaStreamTrack | null;
  start: (route: VoiceRoute) => Promise<void>;
  /** Send another input (a deviceId) while talking, without reconnecting. */
  switchMic: (deviceId: string) => Promise<void>;
  /** Switch listening or talking without reconnecting. */
  setRoute: (route: VoiceRoute) => Promise<void>;
  stop: () => Promise<void>;
}

const OFF: VoiceRoute = { listen: false, talk: false, hear: false, apps: false };
/** Either way of talking needs this browser's mic. */
const speaks = (r: VoiceRoute) => r.talk || r.apps;

async function microphone(deviceId?: string): Promise<MediaStreamTrack> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, ...(deviceId ? { deviceId: { exact: deviceId } } : {}) },
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
 * through the controller. Hiding the page doesn't end it (whether a locked
 * phone keeps streaming is up to its browser); polling stops while hidden
 * and, back in view, notices a session that ended meanwhile.
 */
export function useVoice(visible: boolean): VoiceApi {
  const [view, setView] = useState<VoiceView | null>(null);
  const [status, setStatus] = useState<VoiceStatus>("off");
  const [route, setRouteState] = useState<VoiceRoute>(OFF);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [mine, setMine] = useState<string | null>(null);
  const [needsTap, setNeedsTap] = useState(false);
  const pc = useRef<RTCPeerConnection | null>(null);
  const id = useRef<string | null>(null);
  // When `id` was set: a poll sent before then can't know the session.
  const openedAt = useRef(0);
  const mineRef = useRef<string | null>(null);
  const mic = useRef<MediaStreamTrack | null>(null);
  const [micTrack, setMicTrack] = useState<MediaStreamTrack | null>(null);
  // The input picked in the panel, kept for when talking switches back on.
  const micDevice = useRef<string | undefined>(undefined);
  const speaker = useRef<HTMLAudioElement | null>(null);

  const teardown = useCallback((why: string | null = null) => {
    pc.current?.close();
    pc.current = null;
    mic.current?.stop();
    mic.current = null;
    setMicTrack(null);
    if (speaker.current) speaker.current.srcObject = null;
    setNeedsTap(false);
    id.current = null;
    mineRef.current = null;
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
      if (id.current && sent > openedAt.current && next.session?.ref !== mineRef.current) {
        teardown("Audio ended");
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
  useEffect(() => {
    // keepalive: the request outlives the page.
    const end = () => {
      if (id.current) void fetch(paths.voiceEnd(id.current), { method: "DELETE", keepalive: true }).catch(() => {});
    };
    // Closing or leaving the page, not hiding it.
    const leave = (event: PageTransitionEvent) => {
      if (!event.persisted) end();
    };
    window.addEventListener("pagehide", leave);
    return () => {
      window.removeEventListener("pagehide", leave);
      end();
      teardown();
    };
  }, [teardown]);

  const hear = useCallback(() => {
    void speaker.current
      ?.play()
      .then(() => setNeedsTap(false))
      .catch(() => {});
  }, []);

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
        // Safari plays the robot's audio later only if this element first
        // played during the tap that started it (listening captures no mic,
        // which would also allow it).
        speaker.current ??= new Audio();
        const out = speaker.current;
        out.autoplay = true;
        out.muted = !(next.listen || next.hear);
        void out.play().catch(() => {});
        let opened: string | null = null;
        try {
          // Take control now: the mic prompt below can outlast the tap's activation.
          await takeControlIfFree(true);
          // Ask for the mic first: a refusal shouldn't open the robot's.
          const track = speaks(next) ? await microphone(micDevice.current).catch(() => microphone()) : null;
          mic.current = track;
          setMicTrack(track);
          const { id: sid, ref, offer } = await apiJson<VoiceOffer>(paths.voiceSession, { method: "POST", body: JSON.stringify(next) });
          opened = sid;
          id.current = sid;
          mineRef.current = ref;
          setMine(ref);
          openedAt.current = performance.now();
          setStatus("connecting");
          setRouteState(next);
          const peer = new RTCPeerConnection();
          pc.current = peer;
          peer.ontrack = (event) => {
            out.srcObject = event.streams[0] ?? new MediaStream([event.track]);
            out.play().catch(() => setNeedsTap(true));
          };
          // End it on the controller too, so the robot's mic closes and the
          // badge clears now, not when the gateway notices.
          const dropped = () => {
            teardown("The audio connection dropped.");
            void apiJson(paths.voiceEnd(sid), { method: "DELETE" }, { take: false }).catch(() => {});
          };
          let grace: ReturnType<typeof setTimeout> | undefined;
          peer.onconnectionstatechange = () => {
            if (pc.current !== peer) return;
            clearTimeout(grace);
            if (peer.connectionState === "connected") setStatus("live");
            else if (peer.connectionState === "failed") dropped();
            // The controller may have ended it while this page was hidden:
            // don't keep the mic until ICE gives up, but allow a network blip.
            else if (peer.connectionState === "disconnected") {
              grace = setTimeout(() => {
                if (pc.current === peer && peer.connectionState !== "connected") dropped();
              }, DISCONNECT_GRACE_MS);
            }
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
          if (opened) void apiJson(paths.voiceEnd(opened), { method: "DELETE" }, { take: false }).catch(() => {});
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
        if (speaks(next) && !mic.current) {
          // Nothing has changed yet, so a refused mic just leaves the switch off.
          try {
            mic.current = await microphone(micDevice.current).catch(() => microphone());
          } catch (e) {
            setError(errorText(e, "Couldn't switch the audio."));
            return;
          }
        }
        try {
          if (speaks(next)) {
            await sender.replaceTrack(mic.current);
            setMicTrack(mic.current);
          } else if (mic.current) {
            // Release the mic, so the browser's own mic indicator goes off too.
            await sender.replaceTrack(null);
            mic.current.stop();
            mic.current = null;
            setMicTrack(null);
          }
          if (speaker.current) speaker.current.muted = !(next.listen || next.hear);
          setRouteState(next);
          await apiJson(paths.voiceRoute(sid), { method: "POST", body: JSON.stringify(next) });
        } catch (e) {
          // A failed reply doesn't say whether the switch applied, on or off, so
          // the switches can't be trusted: stop it all.
          teardown();
          void apiJson(paths.voiceEnd(sid), { method: "DELETE" }, { take: false }).catch(() => {});
          setError(`${errorText(e, "Couldn't switch the audio.")} Audio stopped.`);
        }
      }),
    [run, teardown],
  );

  const stop = useCallback(
    () =>
      run(async () => {
        const sid = id.current;
        teardown();
        if (sid) await apiJson(paths.voiceEnd(sid), { method: "DELETE" }, { take: false }).catch(() => {});
      }),
    [run, teardown],
  );

  const switchMic = useCallback(
    (deviceId: string) =>
      run(async () => {
        const sender = pc.current?.getTransceivers()[0]?.sender;
        if (!sender || !mic.current) return;
        let next: MediaStreamTrack;
        try {
          next = await microphone(deviceId);
        } catch (e) {
          // The old mic is still sending.
          setError(errorText(e, "Couldn't switch the microphone."));
          return;
        }
        // Stopped or switched off meanwhile: drop the new one.
        if (pc.current?.getTransceivers()[0]?.sender !== sender || !mic.current) {
          next.stop();
          return;
        }
        try {
          await sender.replaceTrack(next);
        } catch (e) {
          next.stop();
          setError(errorText(e, "Couldn't switch the microphone."));
          return;
        }
        // The session may have ended while the swap was pending.
        if (pc.current?.getTransceivers()[0]?.sender !== sender || !mic.current) {
          next.stop();
          return;
        }
        mic.current.stop();
        mic.current = next;
        micDevice.current = deviceId;
        setMicTrack(next);
      }),
    [run],
  );

  return { view, mine, needsTap, hear, status, route, error, busy, micTrack, start, switchMic, setRoute, stop };
}
